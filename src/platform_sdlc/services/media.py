"""媒体域服务（对应 MediaService）：内容寻址存储，文件在 media-dir/{reqId}/{mediaId}。"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

from platform_sdlc.common import ApiException, Times
from platform_sdlc.db import Database
from platform_sdlc.models import MediaFile
from platform_sdlc.repositories import Repos

_MEDIA_ID = re.compile(r"^m-[0-9a-f]{12}$")
# 序号位数不设上限（next_id 过 999 后是 4 位起，{3} 定长会把新卡媒体读写全拒——verify-m4 实测踩中）
_REQ_ID = re.compile(r"^REQ-[0-9]{4}-[0-9]{3,}$")
# 可执行/可脚本内容拒绝入库（防存储型同源 XSS：响应端只对图片白名单内联，双防线）
_DANGEROUS_MIME = {"text/html", "image/svg+xml", "application/xhtml+xml", "text/xml", "application/xml"}
_DEFAULT_MAX_BYTES = 20 * 1024 * 1024


@dataclass
class MediaData:
    meta: MediaFile
    data: bytes


class MediaService:
    def __init__(self, db: Database, repos: Repos, media_dir: Path,
                 max_bytes: int = _DEFAULT_MAX_BYTES) -> None:
        self.db = db
        self.repos = repos
        self.media_dir = media_dir
        self.max_bytes = max_bytes

    def upload(self, req_id: str, name: str, mime: str, data: bytes, explicit_id: str | None = None) -> MediaFile:
        self._check_req(req_id)
        if not data:
            raise ApiException(400, "媒体内容为空")
        if len(data) > self.max_bytes:
            raise ApiException(413, f"媒体过大（上限 {self.max_bytes // (1024 * 1024)}MB）")
        if (mime or "").split(";")[0].strip().lower() in _DANGEROUS_MIME:
            raise ApiException(400, f"不允许的媒体类型：{mime}（可执行/可脚本内容拒绝入库）")
        sha = hashlib.sha256(data).hexdigest()
        if explicit_id:
            if not _MEDIA_ID.match(explicit_id):
                raise ApiException(400, f"非法 mediaId：{explicit_id}")
            media_id = explicit_id
        else:
            media_id = "m-" + sha[:12]
        # find→insert 同事务：并发同 mediaId 双过检查后第二笔撞 (req_id,media_id) 唯一键的 500 被堵住
        # （RLock 串行：后进者在锁内 find 命中先提交者 → 幂等返回）
        with self.db.tx():
            existing = self.repos.media.find(req_id, media_id)
            if existing is not None:
                # 同 id 不同内容 = 调用方误用（显式 mediaId 的内容契约是稳定的）——拒绝而非
                # 静默沿用旧值（历史行 sha 为空时无从比对，保持幂等返回）
                if existing.sha256 and existing.sha256 != sha:
                    raise ApiException(409, f"mediaId 已被不同内容占用：{media_id}"
                                        "（显式 id 的内容须稳定；换内容请换 id）")
                return existing

            f = MediaFile(req_id=req_id, media_id=media_id, sha256=sha, name=_safe_name(name),
                          mime=_safe_mime(mime), bytes=len(data), created_at=Times.now())
            try:
                d = self.media_dir / req_id
                d.mkdir(parents=True, exist_ok=True)
                (d / media_id).write_bytes(data)
            except OSError as e:
                raise ApiException(500, f"媒体落盘失败：{e}") from e
            self.repos.media.insert(f)
            return f

    def load(self, req_id: str, media_id: str) -> MediaData:
        """读取媒体（文件 + 元数据）；ETag = sha256 供 304 协商缓存（由路由层使用）。"""
        self._check_req(req_id)
        if not _MEDIA_ID.match(media_id):
            raise ApiException(400, "非法 mediaId")
        f = self.repos.media.find(req_id, media_id)
        if f is None:
            raise ApiException(404, f"媒体不存在：{media_id}")
        p = self.media_dir / req_id / media_id
        if not p.is_file():
            raise ApiException(404, f"媒体文件缺失：{media_id}")
        try:
            return MediaData(meta=f, data=p.read_bytes())
        except OSError as e:
            raise ApiException(500, f"媒体读取失败：{e}") from e

    def _check_req(self, req_id: str) -> None:
        if not _REQ_ID.match(req_id):
            raise ApiException(400, "非法需求编号")
        if self.repos.requirement.find(req_id) is None:
            raise ApiException(404, f"需求不存在：{req_id}")


def _safe_name(name: str | None) -> str:
    n = re.sub(r'[\r\n"/\\\\]', "", name or "")
    return n[-200:] if len(n) > 200 else n


def _safe_mime(mime: str | None) -> str:
    m = (mime or "").strip()
    ok = re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9!#$&^_.+-]{0,127}/[a-zA-Z0-9][a-zA-Z0-9!#$&^_.+-]{0,127}", m)
    return m if ok else "application/octet-stream"
