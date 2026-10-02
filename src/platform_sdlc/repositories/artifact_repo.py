"""产物仓储（对应 ArtifactRepoImpl）。"""

from __future__ import annotations

from platform_sdlc.db import Database
from platform_sdlc.models import Artifact


class ArtifactRepo:
    def __init__(self, db: Database) -> None:
        self.db = db

    def upsert(self, req_id: str, type_: str, markdown: str, content_json: str, at: str) -> None:
        """重复推送 = 覆盖并 version+1。"""
        self.db.execute(
            "INSERT INTO artifact(req_id,type,markdown,content_json,version,pushed_at) VALUES(?,?,?,?,1,?) "
            "ON CONFLICT(req_id,type) DO UPDATE SET markdown=excluded.markdown, content_json=excluded.content_json, "
            "version=artifact.version+1, pushed_at=excluded.pushed_at",
            (req_id, type_, markdown, content_json, at),
        )

    def find(self, req_id: str, type_: str) -> Artifact | None:
        row = self.db.query_one("SELECT * FROM artifact WHERE req_id=? AND type=?", (req_id, type_))
        return Artifact.from_row(row) if row else None
