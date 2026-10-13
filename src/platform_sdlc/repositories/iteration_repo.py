"""迭代仓储（对应 IterationRepoImpl）：绑定需求摘要与设计/门禁阶段推导（D33）。"""

from __future__ import annotations

from platform_sdlc.db import Database
from platform_sdlc.models import Iteration, IterationListRow, Row
from platform_sdlc.states import CLARIFY_DISPOSED_SQL

# 绑定需求摘要子查询（D33）：最新一条存活行；聚合口径与 RequirementRepo.list_all 一致
_REQ_JOIN = (
    " LEFT JOIN requirement r ON r.id = ("
    "   SELECT r2.id FROM requirement r2 WHERE r2.iter = i.name AND r2.deleted_at=''"
    "   ORDER BY r2.created_at DESC, r2.id DESC LIMIT 1)"
)

_ROW_SELECT = (
    "SELECT i.name, i.note, i.created_by, i.created_at,"
    " r.id AS req_id, r.name AS req_name, r.status AS req_status, r.risk_tier, r.submitted_by,"
    " (SELECT COUNT(*) FROM clarify_item c WHERE c.req_id=r.id) AS total_q,"
    " (SELECT COUNT(*) FROM clarify_item c WHERE c.req_id=r.id AND c.status IN " + CLARIFY_DISPOSED_SQL + ") AS answered_q,"
    " (SELECT COUNT(*) FROM test_case tc WHERE tc.req_id=r.id) AS case_count,"
    " CASE WHEN r.id IS NULL THEN 'none'"
    "  WHEN r.design_final_json LIKE '%\"skipDeclared\"%' THEN 'skipped'"
    "  WHEN r.design_final_json LIKE '%\"finalized\"%' THEN 'final'"
    "  WHEN EXISTS (SELECT 1 FROM draft_artifact da WHERE da.req_id=r.id AND da.type='design') THEN 'draft'"
    "  ELSE 'none' END AS design_phase,"
    " CASE WHEN r.id IS NULL THEN 'none'"
    "  WHEN EXISTS (SELECT 1 FROM gate_round gr WHERE gr.req_id=r.id AND gr.status='released'"
    "    AND gr.round_no=(SELECT MAX(gr2.round_no) FROM gate_round gr2 WHERE gr2.req_id=r.id)) THEN 'released'"
    "  WHEN EXISTS (SELECT 1 FROM gate_round gr WHERE gr.req_id=r.id) THEN 'active'"
    "  ELSE 'none' END AS gate_stage"
    " FROM iteration i" + _REQ_JOIN
)


def _row_to_list_row(row: Row) -> IterationListRow:
    return IterationListRow(
        name=str(row["name"]),
        note=str(row["note"] or ""),
        created_by=str(row["created_by"] or ""),
        created_at=str(row["created_at"] or ""),
        req_id=None if row["req_id"] is None else str(row["req_id"]),
        req_name=None if row["req_name"] is None else str(row["req_name"]),
        req_status=None if row["req_status"] is None else str(row["req_status"]),
        risk_tier=str(row["risk_tier"] or ""),
        submitted_by=str(row["submitted_by"] or ""),
        total_q=int(row["total_q"] or 0),
        answered_q=int(row["answered_q"] or 0),
        case_count=int(row["case_count"] or 0),
        design_phase=str(row["design_phase"] or "none"),
        gate_stage=str(row["gate_stage"] or "none"),
    )


class IterationRepo:
    def __init__(self, db: Database) -> None:
        self.db = db

    def insert(self, it: Iteration) -> None:
        self.db.execute(
            "INSERT INTO iteration(name,note,created_by,created_at) VALUES(?,?,?,?)",
            (it.name, it.note, it.created_by, it.created_at),
        )

    def exists(self, name: str) -> bool:
        n = self.db.query_scalar("SELECT COUNT(*) FROM iteration WHERE name=?", (name,))
        return int(n or 0) > 0

    def find(self, name: str) -> Iteration | None:
        row = self.db.query_one("SELECT * FROM iteration WHERE name=?", (name,))
        return Iteration.from_row(row) if row else None

    def list_all(self) -> list[IterationListRow]:
        # LIMIT 200 防御上限（AUDIT D2-1）：行级子查询密集（每行 5+），无界列表在数据量增长后线性劣化
        return [_row_to_list_row(r) for r in self.db.query_all(
            _ROW_SELECT + " ORDER BY i.created_at DESC, i.name DESC LIMIT 200")]

    def find_row(self, name: str) -> IterationListRow | None:
        row = self.db.query_one(_ROW_SELECT + " WHERE i.name=?", (name,))
        return _row_to_list_row(row) if row else None

    def delete(self, name: str) -> int:
        return self.db.execute("DELETE FROM iteration WHERE name=?", (name,))

    def ensure(self, name: str, created_at: str) -> None:
        self.db.execute(
            "INSERT INTO iteration(name, created_at) SELECT ?, ? WHERE NOT EXISTS (SELECT 1 FROM iteration WHERE name=?)",
            (name, created_at, name),
        )
