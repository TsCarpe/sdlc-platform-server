"""媒体端点（对应 MediaController）：raw bytes 上传 / ETag 协商缓存读取。

服务调用一律 asyncio.to_thread：load 的整文件读盘若跑在事件循环上，会冻结 agent 轮询与 SSE。"""

from __future__ import annotations

import asyncio
from urllib.parse import quote

from fastapi import APIRouter, Request, Response

from platform_sdlc.common import ApiException
from platform_sdlc.runtime import AppContext

# 仅图片白名单内联渲染；其余一律按附件下载——SPA 与 API 同源托管，text/html 等内联即存储型 XSS
#（<img src> 加载不受 Content-Disposition 影响，图片内联展示不变）
_INLINE_MIME = {"image/png", "image/jpeg", "image/gif", "image/webp"}


def router(ctx: AppContext) -> APIRouter:
    r = APIRouter()
    svc = ctx.services.media

    @r.post("/api/requirements/{req_id}/media")
    async def upload(req_id: str, request: Request, name: str = "", mediaId: str = "") -> dict[str, object]:
        # Content-Length 预检：超限在读 body 前拒绝（body 全量进内存，预检堵放大面；服务层 len 兜底）
        cl = request.headers.get("content-length")
        if cl and cl.isdigit() and int(cl) > ctx.settings.media_max_bytes:
            raise ApiException(413, f"媒体过大（上限 {ctx.settings.media_max_bytes // (1024 * 1024)}MB）")
        body = await request.body()
        mime = request.headers.get("x-media-mime", "")
        f = await asyncio.to_thread(svc.upload, req_id, name, mime, body, explicit_id=mediaId or None)
        # MediaUploadResult 字段声明序 = 既有契约键序
        return {"id": f.media_id, "sha256": f.sha256, "bytes": f.bytes, "name": f.name, "mime": f.mime}

    @r.get("/api/requirements/{req_id}/media/{media_id}")
    async def load(req_id: str, media_id: str, request: Request) -> Response:
        d = await asyncio.to_thread(svc.load, req_id, media_id)
        etag = f'"{d.meta.sha256}"'
        if request.headers.get("if-none-match") == etag:
            return Response(status_code=304, headers={"ETag": etag})
        media_type = d.meta.mime if d.meta.mime and "/" in d.meta.mime else "application/octet-stream"
        headers = {"ETag": etag, "Cache-Control": "private, max-age=31536000, immutable",
                   "X-Content-Type-Options": "nosniff"}
        if media_type not in _INLINE_MIME:
            fname = quote(d.meta.name or media_id)
            headers["Content-Disposition"] = f"attachment; filename*=UTF-8''{fname}"
        return Response(content=d.data, media_type=media_type, headers=headers)

    return r
