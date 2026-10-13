"""用例评论仓储（对应 CommentRepoImpl）。"""

from __future__ import annotations

from platform_sdlc.db import Database
from platform_sdlc.models import CaseComment


class CommentRepo:
    def __init__(self, db: Database) -> None:
        self.db = db

    def insert(self, c: CaseComment) -> None:
        self.db.execute(
            "INSERT INTO case_comment(req_id,case_id,by,text,created_at) VALUES(?,?,?,?,?)",
            (c.req_id, c.case_id, c.by, c.text, c.created_at),
        )

    def list_by_case(self, req_id: str, case_id: str) -> list[CaseComment]:
        return [
            CaseComment.from_row(r)
            for r in self.db.query_all("SELECT * FROM case_comment WHERE req_id=? AND case_id=? ORDER BY id", (req_id, case_id))
        ]

    def delete_by_case(self, req_id: str, case_id: str) -> None:
        """用例被同步删除时清理其评论（孤儿评论不留）。"""
        self.db.execute("DELETE FROM case_comment WHERE req_id=? AND case_id=?", (req_id, case_id))

    def delete_by_cases(self, req_id: str, case_ids: list[str]) -> None:
        """批量级联清理（与 TestCaseRepo.delete_many 配套，AUDIT D2-4）。"""
        if not case_ids:
            return
        marks = ",".join("?" * len(case_ids))
        self.db.execute(f"DELETE FROM case_comment WHERE req_id=? AND case_id IN ({marks})", [req_id, *case_ids])
