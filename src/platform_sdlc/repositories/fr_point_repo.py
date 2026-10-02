"""功能点仓储（对应 FrPointRepoImpl）。"""

from __future__ import annotations

from platform_sdlc.db import Database
from platform_sdlc.models import FrPoint, Row


class FrPointRepo:
    def __init__(self, db: Database) -> None:
        self.db = db

    def replace_all(self, req_id: str, points: list[FrPoint]) -> None:
        self.db.execute("DELETE FROM fr_point WHERE req_id=?", (req_id,))
        for p in points:
            self.db.execute(
                "INSERT INTO fr_point(req_id,id,name,source,group_name,module,anchor) VALUES(?,?,?,?,?,?,?)",
                (req_id, p.id, p.name, p.source, p.group, p.module, p.anchor),
            )

    def list_by_req(self, req_id: str) -> list[FrPoint]:
        return [self._from_row(r) for r in self.db.query_all("SELECT * FROM fr_point WHERE req_id=? ORDER BY id", (req_id,))]

    def exists(self, req_id: str, point_id: str) -> bool:
        n = self.db.query_scalar("SELECT COUNT(*) FROM fr_point WHERE req_id=? AND id=?", (req_id, point_id))
        return int(n or 0) > 0

    @staticmethod
    def _from_row(row: Row) -> FrPoint:
        return FrPoint(
            req_id=str(row["req_id"]),
            id=str(row["id"]),
            name=str(row["name"] or ""),
            source=str(row["source"] or ""),
            group=str(row["group_name"] or ""),
            module=str(row["module"] or ""),
            anchor=str(row["anchor"] or ""),
        )
