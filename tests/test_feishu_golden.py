"""飞书新链路离线黄金比对（DESIGN-飞书接入Python化 §10 验收 #2）。

链路（纯函数 + 离线 fixture，不打真网）：docsai.xml + comments.json（原始评论响应）
→ select_comments（拍板筛选）→ build_refmap（cN 锚点位置序）→ inject_comment_refs
→ parse_doc（姊妹仓）→ doc.json′，与冻结黄金 doc.json 比对：

- 正文块逐块比 id/type/text/caption/path（验收判据面）
- media id 集合相等
- comment 块子集校验：黄金每条（按 comment_id）在新产物中存在且 anchorBlockId/quote/thread
  一致——新链路按拍板规则多收（黄金 = lark-cli is_solved 规则的子集），ref 键因筛选集
  不同允许漂移（断言 c\\d+ 格式）
- 用户名：离线测试以黄金 refmap 反推 user_id→名字映射（真实链路由 contact API 提供，
  失败降级 user_id 前 8 位，§4）

姊妹仓 parse_doc.py 缺席时整组 skip（CI 无姊妹仓环境的先例见 test_v5_clarify 的 live 对齐）。
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, cast

import pytest

from platform_sdlc.integrations.feishu.comments import build_refmap, select_comments
from platform_sdlc.integrations.feishu.docx import inject_comment_refs

FIXTURES = Path(__file__).parent / "fixtures" / "feishu"
PARSE_DOC = Path.home() / "IdeaProjects/claude-sdlc-skills/skills/sdlc-intent/scripts/parse_doc.py"
SAMPLES = sorted(p.name for p in FIXTURES.iterdir() if p.is_dir()) if FIXTURES.is_dir() else []

pytestmark = pytest.mark.skipif(not PARSE_DOC.is_file(), reason="姊妹仓 parse_doc.py 不在本机")


def _jobj(v: Any) -> dict[str, Any]:
    return cast("dict[str, Any]", v) if isinstance(v, dict) else {}


def _jlist(v: Any) -> list[Any]:
    return cast("list[Any]", v) if isinstance(v, list) else []


def _load(sample: str, name: str) -> Any:
    return json.loads((FIXTURES / sample / name).read_text(encoding="utf-8"))


def _names_from_golden(golden_refmap: dict[str, Any], comments: list[dict[str, Any]]) -> dict[str, str]:
    """黄金 refmap 反推 user_id→名字（contact 替身）：
    ① msg user 属性 ↔ replies[i].user_id（回复作者）
    ② msg 文本内 @token ↔ 同一 reply 的 person.user_id 序列（被 @ 者）"""
    names: dict[str, str] = {}
    for entry_raw in golden_refmap.values():
        data = str(_jobj(entry_raw).get("data") or "")
        m = re.search(r'comment-id="(\d+)"', data)
        if not m:
            continue
        hit = next((x for x in comments if str(x.get("comment_id")) == m.group(1)), None)
        if hit is None:
            continue
        replies = [_jobj(r) for r in _jlist(_jobj(hit.get("reply_list")).get("replies"))]
        msgs = re.findall(r'<msg user="([^"]*)">(.*?)</msg>', data)
        for (author, body), reply in zip(msgs, replies, strict=False):
            uid = str(reply.get("user_id") or "")
            if uid and author and not author.startswith(uid[:8]):
                names[uid] = author
            elements = _jlist(_jobj(reply.get("content")).get("elements"))
            persons = [str(_jobj(e).get("person") and _jobj(_jobj(e).get("person")).get("user_id") or "")
                       for e in elements if _jobj(e).get("person")]
            mentions = re.findall(r"@([\w\d\-·一-鿿]+)", body)
            for pid, name in zip(persons, mentions, strict=False):
                if pid and name and not name.startswith(pid[:8]):
                    names[pid] = name
    return names


def _run_parse_doc(tmp: Path, sample: str, xml: str, refmap: dict[str, Any]) -> dict[str, Any]:
    (tmp / "content.xml").write_text(xml, encoding="utf-8")
    (tmp / "refmap.json").write_text(json.dumps(refmap, ensure_ascii=False), encoding="utf-8")
    meta = _jobj(_load(sample, "meta.json"))
    out = tmp / "doc.json"
    r = subprocess.run(
        [sys.executable, str(PARSE_DOC), "--xml", str(tmp / "content.xml"),
         "--refmap", str(tmp / "refmap.json"), "--out", str(out),
         "--media-dir", str(tmp / "media"), "--url", str(meta.get("url") or ""),
         "--document-id", str(meta.get("document_id") or ""),
         "--revision-id", str(meta.get("revision_id") or "")],
        capture_output=True, text=True, timeout=120, check=False)
    assert r.returncode == 0, f"parse_doc 失败：\n{r.stdout}\n{r.stderr}"
    return _jobj(json.loads(out.read_text(encoding="utf-8")))


def _block_key(b: dict[str, Any]) -> str:
    """正文块比对键（验收 #2 判据面：id/type/text/caption/path）。"""
    return json.dumps([b.get("id"), b.get("type"), b.get("text"), b.get("caption"), b.get("path")],
                      ensure_ascii=False)


@pytest.mark.parametrize("sample", SAMPLES)
def test_golden_match(sample: str, tmp_path: Path) -> None:
    docsai = (FIXTURES / sample / "docsai.xml").read_text(encoding="utf-8")
    comments = [_jobj(c) for c in _jlist(_jobj(_load(sample, "comments.json")).get("items"))]
    golden = _jobj(_load(sample, "doc.json"))
    golden_refmap = _jobj(_load(sample, "refmap.json"))

    alive, dropped_orphan = select_comments(comments, docsai)
    refmap, anchor_refs = build_refmap(alive, docsai, _names_from_golden(golden_refmap, comments))
    xml = inject_comment_refs(docsai, anchor_refs)
    produced = _run_parse_doc(tmp_path, sample, xml, refmap)

    # ---- 正文块逐块比对（黄金与产物同源同序；comment 块另走子集校验）----
    def content_blocks(doc: dict[str, Any]) -> list[dict[str, Any]]:
        return [_jobj(b) for b in _jlist(doc.get("blocks")) if _jobj(b).get("type") != "comment"]

    g_blocks, p_blocks = content_blocks(golden), content_blocks(produced)
    assert len(g_blocks) == len(p_blocks), (
        f"正文块数不符：黄金 {len(g_blocks)} vs 产物 {len(p_blocks)}")
    for g, p in zip(g_blocks, p_blocks, strict=True):
        assert _block_key(g) == _block_key(p), f"块不符：黄金 {_block_key(g)[:160]} vs 产物 {_block_key(p)[:160]}"

    # ---- media id 集合 ----
    g_media = {str(_jobj(m).get("id")) for m in _jlist(golden.get("media"))}
    p_media = {str(_jobj(m).get("id")) for m in _jlist(produced.get("media"))}
    assert g_media == p_media, f"media id 集合不符：仅黄金 {g_media - p_media}，仅产物 {p_media - g_media}"

    # ---- comment 块子集校验（拍板规则多收为预期；锚点孤儿丢弃记数）----
    def comments_of(doc: dict[str, Any]) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        for b in _jlist(doc.get("blocks")):
            if _jobj(b).get("type") == "comment":
                out[str(_jobj(b).get("id"))] = _jobj(b)
        return out

    g_comments, p_comments = comments_of(golden), comments_of(produced)
    assert len(g_comments) <= len(p_comments), "新链路评论少于黄金（拍板规则应多收）"
    for cid, g in g_comments.items():
        p = p_comments.get(cid)
        assert p is not None, f"黄金评论 {cid} 未被新链路收录"
        assert g.get("anchorBlockId") == p.get("anchorBlockId"), f"评论 {cid} 锚点不符"
        assert g.get("quote") == p.get("quote"), f"评论 {cid} quote 不符"
        assert g.get("thread") == p.get("thread"), (
            f"评论 {cid} thread 不符：{g.get('thread')} vs {p.get('thread')}")
        assert re.fullmatch(r"c\d+", str(p.get("ref") or "")), f"评论 {cid} ref 键格式异常：{p.get('ref')}"
    # 多收面可解释性：产物评论全部来自拍板筛选的存活线程
    assert len(p_comments) == len(alive), (
        f"产物评论数 {len(p_comments)} 与存活线程 {len(alive)} 不匹配（孤儿丢弃 {dropped_orphan}）")


def test_select_comments_filters_deleted_and_orphan() -> None:
    """拍板规则的筛选单测（构造用例：已删/无锚点/孤儿锚点/存活四形态）。"""
    xml = '<body><p id="blk-a">A</p><p id="blk-b">B</p></body>'
    raw: list[dict[str, Any]] = [
        {"comment_id": "1", "extra": {"content_anchor_id": "blk-a"},
         "relation": {"content_deleted": False, "relation": "{}"}, "quote": "q1",
         "reply_list": {"replies": []}},
        {"comment_id": "2", "extra": {"content_anchor_id": "blk-a"},
         "relation": {"content_deleted": True, "relation": "{}"}, "quote": "q2",
         "reply_list": {"replies": []}},
        {"comment_id": "3", "extra": {}, "relation": {"content_deleted": False}, "quote": "q3",
         "reply_list": {"replies": []}},
        {"comment_id": "4", "extra": {"content_anchor_id": "blk-gone"},
         "relation": {"content_deleted": False, "relation": "{}"}, "quote": "q4",
         "reply_list": {"replies": []}},
    ]
    alive, dropped = select_comments(raw, xml)
    assert [e.comment_id for e in alive] == ["1"]
    assert dropped == 1
