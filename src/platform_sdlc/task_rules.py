"""任务通道共享规则（AUDIT D1-5，批次7）：双执行通道（进程内 loop / 外置 runner.mjs）的
单一数据源——任务→草稿映射、机械自查阈值、结果行判据。

经 GET /api/meta/task-rules 下发，runner 启动拉取（失败回退本地同款默认）；
阈值/映射变更只改此处，双通道不再各自漂移。判定文案与提示语留在各自实现（受众不同，不模板化）。
"""

from __future__ import annotations

# 任务类型 → 回传草稿类型
DRAFT_TYPES: dict[str, str] = {
    "quality_check": "digest_bundle",
    "standard_doc": "standard",
    "design_gen": "design",
    "design_rev": "design",
    "gate_review": "gate_issues",
    "case_gen": "cases",
}

# 机械自查阈值（done 判定的齐套下限；文案见 loop.self_check / runner.mjs selfCheck 各自实现）
SELF_CHECK: dict[str, dict[str, object]] = {
    "standard": {"minMd": 200},
    "design": {"minMd": 200, "requireHeading": "§0"},
    "gate_issues": {"minMd": 100, "issuesNonEmpty": True},
    "digest_bundle": {"structure": ("digest", "audit", "items"), "itemsNonEmpty": True},
    "cases": {"structure": ("items",), "itemsNonEmpty": True},
}

# 结果行判据（SDLC_TASK_RESULT {json} 收尾行；两端均不强制——进程内有续跑强化，runner 仅警示）
RESULT_LINE_PATTERN = r"SDLC_TASK_RESULT\s+(\{.*\})\s*$"


def task_rules_payload() -> dict[str, object]:
    """端点出参（runner 消费面）。"""
    return {
        "draftTypes": DRAFT_TYPES,
        "selfCheck": SELF_CHECK,
        "resultLinePattern": RESULT_LINE_PATTERN,
    }
