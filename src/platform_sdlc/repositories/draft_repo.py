"""草稿仓储（对应 DraftRepoImpl）：ON CONFLICT 全量覆盖。"""

from __future__ import annotations

from platform_sdlc.db import Database
from platform_sdlc.models import DraftArtifact


class DraftRepo:
    def __init__(self, db: Database) -> None:
        self.db = db

    def upsert(self, d: DraftArtifact, at: str) -> None:
        self.db.execute(
            "INSERT INTO draft_artifact(req_id,type,md,content_json,task_id,updated_at) VALUES(?,?,?,?,?,?) "
            "ON DUPLICATE KEY UPDATE md=VALUES(md), content_json=VALUES(content_json), "
            "task_id=VALUES(task_id), updated_at=VALUES(updated_at)",
            (d.req_id, d.type, d.md, d.content_json, d.task_id, at),
        )

    def find(self, req_id: str, type_: str) -> DraftArtifact | None:
        row = self.db.query_one("SELECT * FROM draft_artifact WHERE req_id=? AND type=?", (req_id, type_))
        return DraftArtifact.from_row(row) if row else None

    def list_by_req(self, req_id: str) -> list[DraftArtifact]:
        return [
            DraftArtifact.from_row(r) for r in self.db.query_all("SELECT * FROM draft_artifact WHERE req_id=? ORDER BY type", (req_id,))
        ]

    def delete(self, req_id: str, type_: str) -> int:
        return self.db.execute("DELETE FROM draft_artifact WHERE req_id=? AND type=?", (req_id, type_))
