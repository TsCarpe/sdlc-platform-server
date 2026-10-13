"""状态与枚举常量收口（AUDIT D3-1/D3-3）：流转表 + 全域状态/类型/等级常量。

行为零变更：仅把散布各守卫点的字面量收口为同一来源（拼写错误从运行期失败变为导入期失败）；
错误文案逐字保留。require 404 的四份同构复制暂不收口（分层收益/风险不划算，见 AUDIT 报告 D3-2 备注）。
"""

from __future__ import annotations

# ---------- 需求域状态机 ----------

REQ_SUBMITTED = "submitted"
REQ_REVIEWING = "reviewing"
REQ_CLARIFYING = "clarifying"
REQ_PUBLISHED = "published"  # 终态（发布终态锁：拒推/拒草稿/拒任务/拒作答）

# set_status 通道的显式流转（push_artifacts 隐式推进 clarifying、publish 终态推进不在此表）
REQ_FLOW: frozenset[tuple[str, str]] = frozenset({
    (REQ_SUBMITTED, REQ_REVIEWING),
    (REQ_REVIEWING, REQ_CLARIFYING),
})


def req_can_transition(cur: str, target: str) -> bool:
    return (cur, target) in REQ_FLOW


# ---------- 澄清处置（D21/D25：驳回/挂起计入已处置） ----------

CLARIFY_ANSWERED = "answered"
CLARIFY_REJECTED = "rejected"
CLARIFY_DEFERRED = "deferred"
CLARIFY_DISPOSED: tuple[str, ...] = (CLARIFY_ANSWERED, CLARIFY_REJECTED, CLARIFY_DEFERRED)
# counts/列表 answered 聚合的 IN 集合（值域常量，非用户输入——拼接不破坏参数化红线）
CLARIFY_DISPOSED_SQL = "('answered','rejected','deferred')"

# ---------- 门禁域（批次6 英文化后的权威值） ----------

GATE_ROUND_PENDING = "pending"  # 待裁决
GATE_ROUND_RELEASED = "released"  # 已放行
VERDICT_REVISE = "revise"  # 落改
VERDICT_REJECT = "reject"  # 驳回
VERDICT_QUERY = "query"  # 存疑
VERDICT_FILED = "filed"  # 备案（读面推导值，不落库）
REV_PENDING = "pending"  # 修订待执行
REV_DONE = "done"  # 修订已执行
