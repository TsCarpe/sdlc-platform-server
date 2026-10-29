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
            # MySQL upsert：VALUES(col) 旧语法（5.7~8.x 全系可用，8.0.20+ 仅 deprecation warning）
            "INSERT INTO artifact(req_id,type,markdown,content_json,version,pushed_at) VALUES(?,?,?,?,1,?) "
            "ON DUPLICATE KEY UPDATE markdown=VALUES(markdown), content_json=VALUES(content_json), "
            "version=version+1, pushed_at=VALUES(pushed_at)",
            (req_id, type_, markdown, content_json, at),
        )

    def find(self, req_id: str, type_: str) -> Artifact | None:
        row = self.db.query_one("SELECT * FROM artifact WHERE req_id=? AND type=?", (req_id, type_))
        return Artifact.from_row(row) if row else None
