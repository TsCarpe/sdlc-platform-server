"""澄清项仓储（对应 ClarifyItemRepoImpl）。"""

from __future__ import annotations

from platform_sdlc.db import Database
from platform_sdlc.models import ClarifyItem
from platform_sdlc.states import CLARIFY_DISPOSED_SQL


class ClarifyItemRepo:
    def __init__(self, db: Database) -> None:
        self.db = db

    def list_by_req(self, req_id: str) -> list[ClarifyItem]:
        return [ClarifyItem.from_row(r) for r in self.db.query_all("SELECT * FROM clarify_item WHERE req_id=? ORDER BY id", (req_id,))]

    def disposition_map_by_seq(self, req_id: str) -> dict[str, ClarifyItem]:
        """seq -> 已处置项（有答复或有处置状态；pending 纯净行不携带。空答复的驳回也携带——D23）。"""
        out: dict[str, ClarifyItem] = {}
        for c in self.list_by_req(req_id):
            has_answer = bool(c.answer)
            has_status = bool(c.status) and c.status != "pending"
            if has_answer or has_status:
                out[c.seq] = c
        return out

    def delete_by_req(self, req_id: str) -> None:
        self.db.execute("DELETE FROM clarify_item WHERE req_id=?", (req_id,))

    def insert(self, c: ClarifyItem) -> None:
        self.db.execute(
            "INSERT INTO clarify_item(req_id,seq,level,theme,question,ctx,answer,answered_at,quote,anchor,status,note,origin)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (c.req_id, c.seq, c.level, c.theme, c.question, c.ctx, c.answer, c.answered_at, c.quote, c.anchor, c.status, c.note, c.origin),
        )

    def insert_many(self, items: list[ClarifyItem]) -> None:
        """批量插入（AUDIT D2-4：重推重建清单的循环逐条消除；调用方事务内）。"""
        if not items:
            return
        self.db.executemany(
            "INSERT INTO clarify_item(req_id,seq,level,theme,question,ctx,answer,answered_at,quote,anchor,status,note,origin)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [(c.req_id, c.seq, c.level, c.theme, c.question, c.ctx, c.answer, c.answered_at, c.quote, c.anchor, c.status, c.note, c.origin)
             for c in items],
        )

    def set_disposition(self, req_id: str, seq: str, answer: str, status: str, note: str, at: str) -> int:
        return self.db.execute(
            "UPDATE clarify_item SET answer=?, status=?, note=?, answered_at=? WHERE req_id=? AND seq=?",
            (answer, status, note, at, req_id, seq),
        )

    def counts(self, req_id: str) -> dict[str, int]:
        """answered = 已处置数（status 口径，D21/D22：显式写入 + 启动回填，无 answer 读时降级）。"""
        row = self.db.query_one(
            "SELECT COUNT(*) AS total, SUM(CASE WHEN status IN " + CLARIFY_DISPOSED_SQL + " THEN 1 ELSE 0 END)"
            " AS answered FROM clarify_item WHERE req_id=?",  # 处置口径与 states.py 同源（D21/D25）
            (req_id,),
        )
        total = int(row["total"]) if row is not None else 0
        answered = int(row["answered"] or 0) if row is not None else 0
        return {"total": total, "answered": answered}
