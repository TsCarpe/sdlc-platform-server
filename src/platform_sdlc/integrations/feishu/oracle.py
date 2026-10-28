"""oracle 独立校验（DESIGN §2/§13：blocks API 原始响应 vs 导入产物 doc.json）。

守恒自检（parse_doc）只保证「XML 内部自洽」——序列化/注入环节漏内容时两侧同缺、恒真。
oracle 是唯一能抓「产物相对 API 漏内容」的运行时门禁（验收 #3：故意删块必须红）。

三组事实（双侧独立计算后比对）：
1. 内容块计数：API block_type → doc.json 类型名映射（容器块不计——内容由子块承载；
   divider 不产块；view(33) 产 attachment；table 产 table-row × row_size）
2. 文本字符多重集合：**单向包含**——API 侧（text_run.content + equation.content）⊆ 产物侧
   （块 text/caption）。单向的原因：mention 用户名、文档标题、图片 alt 仅 docs_ai 侧存在，
   blocks API 无此信息（§13 双服务端链路差异面）；反方向的多出/双计由守恒自检负责
3. media token 集合：image/file/board 的 token（sheet 无下载语义不计）

fail-closed：API 出现未覆盖 block_type 直接报错（映射表外类型无法计数也无法证明被
序列化覆盖）——错误带类型号与块 ID，任务立即失败。
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Any, cast

from platform_sdlc.integrations.feishu.client import as_dict

# 产内容块的类型（API block_type → doc.json 类型名；产块计数口径）
_LEAF_MAP: dict[int, str] = {
    1: "title", 2: "paragraph", 12: "list-item", 13: "list-item", 14: "code", 15: "quote",
    17: "todo", 23: "attachment", 27: "image", 30: "sheet", 43: "whiteboard", 34: "quote",
}
for _t in range(3, 12):  # heading1-9
    _LEAF_MAP[_t] = "heading"

# 容器/结构块：内容由子块承载或自身无内容（不产内容块；计数面排除）。view(33) 归容器：
# 产 attachment 块的是其 child file(23)（file 已按块计数，view 再计即双计——region-score 实证）
_CONTAINER_TYPES = {19, 22, 24, 25, 31, 32, 33}

# 文本承载类型（API 侧文本流来源）
_TEXT_TYPES = {2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 17, 34}

# media token 来源类型的内联字段名
_MEDIA_FIELD = {27: "image", 23: "file", 43: "board"}


class OracleMismatch(Exception):
    """oracle 门禁失败（fetch 链路据此 fail 任务，携带差异清单）。"""


def _norm_counter(text: str) -> Counter[str]:
    """守恒同款归一化（剔 * ~ 与全部空白）：b→** 标记与空格压缩差异双侧豁免。"""
    return Counter(c for c in text if c not in "*~" and not c.isspace())


@dataclass
class Facts:
    leaf_counts: Counter[str] = field(default_factory=lambda: Counter[str]())
    table_rows: int = 0
    text_chars: Counter[str] = field(default_factory=lambda: Counter[str]())
    media_tokens: set[str] = field(default_factory=lambda: set[str]())


def _text_payload(block: dict[str, Any]) -> str:
    """文本承载块的纯文本（text_run.content + equation.content；mention 无文本与产物侧
    cite 展开形成 §13 已知单向差异——oracle 文本面即按单向包含设计）。"""
    data = next((v for k, v in block.items() if k in
                 ("text", "heading1", "heading2", "heading3", "heading4", "heading5", "heading6",
                  "heading7", "heading8", "heading9", "bullet", "ordered", "code", "quote",
                  "todo")), None)
    data = as_dict(data)
    parts: list[str] = []
    for el_raw in cast("list[Any]", data.get("elements") or []):
        el = as_dict(el_raw)
        run = as_dict(el.get("text_run"))
        if run.get("content"):
            parts.append(str(run["content"]))
        eq = as_dict(el.get("equation"))
        if eq.get("content"):
            parts.append(str(eq["content"]))
    return "".join(parts)


def facts_from_blocks(items: list[dict[str, Any]]) -> Facts:
    """blocks API 原始 items → 三组事实（独立于任何序列化/解析实现）。

    计数口径对齐产物形态（黄金实证）：
    - 宿主吸收文本：parent ∈ {cell(32), list(12/13), text(2)} 的 text 块不独立产块——
      cell 内折进 table-row 的 cells、li/p 内折进宿主 text 或被 docs_ai 渲染为列表
      （块树与渲染树的结构差异面，§13）；文本流面照收，仅计数排除
    - 空文本块不产块；table 首行是 thead 表头（不产 table-row），行数 = row_size - 1"""
    facts = Facts()
    absorb_parents = {str(as_dict(b).get("block_id")) for b in items
                      if int(as_dict(b).get("block_type") or 0) in (2, 12, 13, 32)}
    # 表头排除：每表首行 cell 子块与产物 thead 对称（head 不进文本面也不产块）
    thead_cells: set[str] = set()
    for raw in items:
        b = as_dict(raw)
        if int(b.get("block_type") or 0) != 31:
            continue
        table = as_dict(b.get("table"))
        prop = as_dict(table.get("property"))
        col = int(prop.get("column_size") or 0)
        cells = [c for c in cast("list[Any]", table.get("cells") or []) if isinstance(c, str)]
        thead_cells.update(cells[:col] if col else cells[:1])
    absorb_parents |= thead_cells
    for raw in items:
        b = as_dict(raw)
        t = int(b.get("block_type") or 0)
        bid = str(b.get("block_id") or "?")
        if t not in _LEAF_MAP and t not in _CONTAINER_TYPES:
            raise OracleMismatch(
                f"oracle 未覆盖 block_type={t}（块 {bid}）——fail-closed：请在映射表登记该类型"
                "的产物口径或确认文档不含此类块后重试")
        parent_id = str(b.get("parent_id") or "")
        if t == 2:
            payload = _text_payload(b)
            if payload and parent_id not in absorb_parents:
                facts.leaf_counts["paragraph"] += 1
            if parent_id not in thead_cells:  # 表头文本产物侧在 head（不进文本面），对称排除
                facts.text_chars.update(_norm_counter(payload))
        else:
            if t in _LEAF_MAP:
                facts.leaf_counts[_LEAF_MAP[t]] += 1
            if t in _TEXT_TYPES and t != 1:  # page 无文本（标题在 documents get）
                facts.text_chars.update(_norm_counter(_text_payload(b)))
        if t == 31:
            prop = as_dict(as_dict(b.get("table")).get("property"))
            facts.table_rows += max(int(prop.get("row_size") or 0) - 1, 0)
        field_name = _MEDIA_FIELD.get(t)
        if field_name:
            token = str(as_dict(b.get(field_name)).get("token") or "")
            if token:
                facts.media_tokens.add(token)
    return facts


def facts_from_doc(doc: dict[str, Any]) -> Facts:
    """导入产物 doc.json → 同口径三组事实（parse_doc 产物形态）。"""
    facts = Facts()
    for raw in cast("list[Any]", doc.get("blocks") or []):
        b = as_dict(raw)
        if b.get("type") == "comment":
            continue  # 评论是评论 API 面，不在正文 oracle 口径
        if b.get("type") == "table-row":
            facts.table_rows += 1
            for c in cast("list[Any]", b.get("cells") or []):
                if isinstance(c, str):
                    facts.text_chars.update(_norm_counter(c))
            continue
        kind = str(b.get("type") or "?")
        facts.leaf_counts[kind] += 1
        if isinstance(b.get("text"), str):
            facts.text_chars.update(_norm_counter(b["text"]))
        if isinstance(b.get("caption"), str):
            facts.text_chars.update(_norm_counter(b["caption"]))
    for raw in cast("list[Any]", doc.get("media") or []):
        token = str(as_dict(raw).get("token") or "")
        if token:
            facts.media_tokens.add(token)
    return facts


def compare(api: Facts, doc: Facts) -> list[str]:
    """三组事实比对；空清单 = 通过。

    计数与文本面均**单向（API ⊆ 产物）**：oracle 的使命是抓「产物相对 API 漏内容」；
    产物多出（mention 名字/标题/alt 渲染差异、宿主吸收文本）不在此面——多出/双计由
    守恒自检（XML↔产物自洽）与冻结 docsai 门（XML≡lark-cli）负责，三道门闭环。"""
    problems: list[str] = []
    for name in sorted(api.leaf_counts):
        a, d = api.leaf_counts.get(name, 0), doc.leaf_counts.get(name, 0)
        if a > d:
            problems.append(f"块缺失 {name}：API={a} 产物={d}")
    if api.table_rows > doc.table_rows:
        problems.append(f"表格行缺失：API={api.table_rows} 产物={doc.table_rows}")

    missing = api.text_chars - doc.text_chars
    if missing:
        sample = "".join(sorted(missing))[:24]
        problems.append(f"文本字符缺失（API 有产物无 {sum(missing.values())} 个，样本：{sample}）")

    if api.media_tokens != doc.media_tokens:
        problems.append(
            f"media token 不符：仅 API {sorted(api.media_tokens - doc.media_tokens)}，"
            f"仅产物 {sorted(doc.media_tokens - api.media_tokens)}")
    return problems


def verify(items: list[dict[str, Any]], doc: dict[str, Any]) -> None:
    """运行时门禁入口：不一致抛 OracleMismatch（fetch 链路转任务失败）。"""
    problems = compare(facts_from_blocks(items), facts_from_doc(doc))
    if problems:
        raise OracleMismatch("oracle 三方比对失败：" + "；".join(problems))
