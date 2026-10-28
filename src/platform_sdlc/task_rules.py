"""任务通道共享规则（AUDIT D1-5，批次7）：任务→草稿映射的单一数据源。

原「双执行通道规则下发」面（GET /api/meta/task-rules + SELF_CHECK/RESULT_LINE_PATTERN）
随外置 runner 下线（2026-10-28）退役：消费方仅 runner.mjs；进程内 loop.py 运行时直接
import 本模块（self_check 阈值与结果行判据是 loop/executor 各自实现，不经此处下发）。
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
    "incremental_check": "incremental_bundle",  # v4-14（DESIGN-v4 §6.1）
}

# 不产草稿的任务类型（v4-3，DESIGN-v4 §3）：source_import 产物 = parse_doc 输出文件，由任务完成
# 回调（服务端直读文件字节）落库——selfCheck 的「草稿回传了才记 done」对此类跳过
# （漏登记则 DRAFT_TYPES 映射回退 'cases' 会错查不存在的草稿导致任务永远 failed）
NO_DRAFT_TYPES: frozenset[str] = frozenset({"source_import"})
