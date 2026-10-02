"""媒体仓储（对应 MediaRepoImpl）：文件在 media-dir，DB 存元数据。"""

from __future__ import annotations

from platform_sdlc.db import Database
from platform_sdlc.models import MediaFile


class MediaRepo:
    def __init__(self, db: Database) -> None:
        self.db = db

    def exists(self, req_id: str, media_id: str) -> bool:
        n = self.db.query_scalar("SELECT COUNT(*) FROM media WHERE req_id=? AND media_id=?", (req_id, media_id))
        return int(n or 0) > 0

    def find(self, req_id: str, media_id: str) -> MediaFile | None:
        row = self.db.query_one("SELECT * FROM media WHERE req_id=? AND media_id=?", (req_id, media_id))
        return MediaFile.from_row(row) if row else None

    def insert(self, f: MediaFile) -> None:
        self.db.execute(
            "INSERT INTO media(req_id,media_id,sha256,name,mime,bytes,created_at) VALUES(?,?,?,?,?,?,?)",
            (f.req_id, f.media_id, f.sha256, f.name, f.mime, f.bytes, f.created_at),
        )

    def list_by_req(self, req_id: str) -> list[MediaFile]:
        return [MediaFile.from_row(r) for r in self.db.query_all("SELECT * FROM media WHERE req_id=? ORDER BY media_id", (req_id,))]
