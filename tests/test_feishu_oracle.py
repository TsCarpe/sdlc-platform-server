"""oracle 门禁测试（DESIGN §10 验收 #3：故意破坏序列化/产物 → oracle 必须红）。

fixture 驱动：5 个黄金样本的 blocks.json（API 原始）与 doc.json（产物）必须全过；
再以主样本为基线做破坏性验证——删块/删媒体/删文本三种破坏任一必须被抓住。
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from platform_sdlc.integrations.feishu.oracle import (
    OracleMismatch,
    compare,
    facts_from_blocks,
    facts_from_doc,
    verify,
)

FIXTURES = Path(__file__).parent / "fixtures" / "feishu"
SAMPLES = sorted(p.name for p in FIXTURES.iterdir() if p.is_dir()) if FIXTURES.is_dir() else []


def _jload(sample: str, name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / sample / name).read_text(encoding="utf-8"))  # type: ignore[no-any-return]


@pytest.mark.parametrize("sample", SAMPLES)
def test_oracle_passes_on_golden(sample: str) -> None:
    items = _jload(sample, "blocks.json")["items"]
    doc = _jload(sample, "doc.json")
    verify(items, doc)


def test_oracle_catches_missing_block() -> None:
    """验收 #3：产物删一个内容块 → 计数面必红（守恒盲区——sheet 静默丢类问题的克星）。"""
    doc = _jload("jiaoyan-report", "doc.json")
    broken = copy.deepcopy(doc)
    victim = next(b for b in broken["blocks"] if b.get("type") == "sheet") if any(
        b.get("type") == "sheet" for b in broken["blocks"]) else next(
        b for b in broken["blocks"] if b.get("type") == "whiteboard")
    broken["blocks"] = [b for b in broken["blocks"] if b is not victim]
    problems = compare(facts_from_blocks(_jload("jiaoyan-report", "blocks.json")["items"]),
                       facts_from_doc(broken))
    assert problems, "删块后 oracle 未报差异（门禁失效）"


def test_oracle_catches_missing_media() -> None:
    doc = _jload("jiaoyan-report", "doc.json")
    broken = copy.deepcopy(doc)
    broken["media"] = broken["media"][: len(broken["media"]) // 2]
    problems = compare(facts_from_blocks(_jload("jiaoyan-report", "blocks.json")["items"]),
                       facts_from_doc(broken))
    assert any("media token" in p for p in problems)


def test_oracle_catches_missing_text() -> None:
    doc = _jload("jiaoyan-report", "doc.json")
    broken = copy.deepcopy(doc)
    para = next(b for b in broken["blocks"] if b.get("type") == "paragraph" and len(b.get("text") or "") > 20)
    para["text"] = ""
    problems = compare(facts_from_blocks(_jload("jiaoyan-report", "blocks.json")["items"]),
                       facts_from_doc(broken))
    assert any("文本字符缺失" in p for p in problems)


def test_oracle_fail_closed_on_unknown_type() -> None:
    items = [{"block_id": "x1", "block_type": 18, "parent_id": "p"}]  # 18=bitable 未覆盖
    with pytest.raises(OracleMismatch, match="block_type=18"):
        facts_from_blocks(items)


def test_oracle_text_direction_is_subset() -> None:
    """单向包含语义：产物多出 mention 名字/标题/alt 字符不视为失败（§13 差异豁免面）。"""
    doc = _jload("jiaoyan-report", "doc.json")
    enriched = copy.deepcopy(doc)
    title = next(b for b in enriched["blocks"] if b.get("type") == "title")
    title["text"] = title["text"] + "额外标题字符"
    verify(_jload("jiaoyan-report", "blocks.json")["items"], enriched)
