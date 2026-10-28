"""评论规则与 refmap 生成（DESIGN §4 拍板：可解释规则，先宽后收）。

收录条件 = ``content_deleted == false AND extra.content_anchor_id 非空``——比 lark-cli
（实测规则 = is_solved==false，编号按锚点块文档序）多收已解决但锚定现存块的评论，属预期
（主样本 21 vs 11，差 10 条与设计预告一致）。

产出两个消费面：
- refmap（``--refmap`` 输入，parse_doc emit_comments 据此产 comment 块）：键 cN 按锚点块
  在 XML 中的位置升序（黄金样本实证的单调规则），值.data 是 ``<comment comment-id block-id>``
  XML 串（``<quote>`` + 每条回复一个 ``<msg user>``，契约见 parse_doc.py refmap 解析段）
- anchor_refs（block_id → ["c1",…]）：供 docx.inject_comment_refs 向锚定块注入 comment-refs
  属性（lark-cli 客户端行为复刻；宿主块 comments 字段与黄金比对无涉）
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, cast

from platform_sdlc.integrations.feishu.client import as_dict


def xml_escape(s: str) -> str:
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;"))


def _display_name(user_id: str, names: dict[str, str]) -> str:
    """用户名解析降级链（§4）：缓存命中 → user_id 前 8 位。"""
    return names.get(user_id) or user_id[:8]


def _reply_text(reply: dict[str, Any], names: dict[str, str]) -> str:
    """评论回复内容拼接：text_run 直收、person 展开 @名字（黄金 refmap 同形态）、其余类型跳过。"""
    parts: list[str] = []
    elements_raw = as_dict(reply.get("content")).get("elements")
    if not isinstance(elements_raw, list):
        return ""
    for el_raw in cast("list[Any]", elements_raw):
        el = as_dict(el_raw)
        run = as_dict(el.get("text_run"))
        if run.get("text"):
            parts.append(str(run["text"]))
            continue
        person = as_dict(el.get("person"))
        if person.get("user_id"):
            parts.append("@" + _display_name(str(person["user_id"]), names))
            continue
        link = as_dict(el.get("docs_link"))
        if link.get("url"):
            # 文档链接元素展开为 markdown（黄金 refmap 实证：无 title 字段时以 url 兼任）
            url = str(link["url"])
            parts.append(f"[{url}]({url})")
    return "".join(parts)


@dataclass(frozen=True)
class CommentEntry:
    """筛选存活的一条评论线程（锚点已验证存在于文档）。"""

    comment_id: str
    anchor: str
    quote: str
    replies: list[dict[str, Any]]  # 原始 reply（首条即评论本体），生成 msg 时消费
    create_time: int
    is_solved: bool


def _anchor_of(c: dict[str, Any], xml: str) -> str | None:
    """锚点解析链（黄金实证 region-score 7688607…：两源不一致时 lark-cli 取 relation 侧）：
    relation.relation JSON 串的 positionInfo.blockID（带 index/objVersion 的精确位置锚）优先，
    content_anchor_id 兜底（part- 前缀区域锚或缺失时）。均不在文档内 → None。"""
    rel_raw = str(as_dict(c.get("relation")).get("relation") or "")
    try:
        rel = as_dict(json.loads(rel_raw)) if rel_raw else {}
    except json.JSONDecodeError:
        rel = {}
    for v in rel.values():
        pos = as_dict(as_dict(v).get("positionInfo")).get("blockID")
        if pos and f'id="{pos}"' in xml:
            return str(pos)
    anchor = str(as_dict(c.get("extra")).get("content_anchor_id") or "")
    if anchor and f'id="{anchor}"' in xml:
        return anchor
    return None


def select_comments(raw_comments: list[dict[str, Any]], xml: str) -> tuple[list[CommentEntry], int]:
    """拍板筛选 + 锚点有效性校验（anchor ∉ 文档即丢弃并计数——落库孤儿评论无意义）。"""
    alive: list[CommentEntry] = []
    dropped_orphan = 0
    for raw in raw_comments:
        c = as_dict(raw)
        if as_dict(c.get("relation")).get("content_deleted"):
            continue
        if not str(as_dict(c.get("extra")).get("content_anchor_id") or ""):
            continue
        anchor = _anchor_of(c, xml)
        if anchor is None:
            dropped_orphan += 1
            continue
        replies_raw = as_dict(c.get("reply_list")).get("replies")
        replies: list[dict[str, Any]] = (
            [as_dict(r) for r in cast("list[Any]", replies_raw)]
            if isinstance(replies_raw, list) else [])
        alive.append(CommentEntry(
            comment_id=str(c.get("comment_id") or ""),
            anchor=anchor,
            quote=str(c.get("quote") or ""),
            replies=replies,
            create_time=int(c.get("create_time") or 0),
            is_solved=bool(c.get("is_solved")),
        ))
    return alive, dropped_orphan


def build_refmap(alive: list[CommentEntry], xml: str,
                 names: dict[str, str]) -> tuple[dict[str, dict[str, str]], dict[str, list[str]]]:
    """存活线程 → (refmap, anchor_refs)。cN 按锚点块在 XML 中的位置升序（黄金实证规则）。"""
    ordered = sorted(alive, key=lambda e: xml.find(f'id="{e.anchor}"'))
    refmap: dict[str, dict[str, str]] = {}
    anchor_refs: dict[str, list[str]] = {}
    for n, entry in enumerate(ordered, start=1):
        key = f"c{n}"
        msgs: list[str] = []
        for reply in entry.replies:
            text = _reply_text(reply, names)
            user = _display_name(str(as_dict(reply).get("user_id") or ""), names)
            msgs.append(f'<msg user="{xml_escape(user)}">{xml_escape(text)}</msg>')
        quote = f"<quote>{xml_escape(entry.quote)}</quote>" if entry.quote else ""
        data = (f'<comment comment-id="{xml_escape(entry.comment_id)}" '
                f'block-id="{xml_escape(entry.anchor)}">{quote}{"".join(msgs)}</comment>')
        refmap[key] = {"data": data}
        anchor_refs.setdefault(entry.anchor, []).append(key)
    return refmap, anchor_refs
