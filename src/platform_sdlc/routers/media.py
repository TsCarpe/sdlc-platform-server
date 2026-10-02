"""媒体端点（对应 MediaController）：raw bytes 上传 / ETag 协商缓存读取。

服务调用一律 asyncio.to_thread：load 的整文件读盘若跑在事件循环上，会冻结 agent 轮询与 SSE。"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Request, Response

from platform_sdlc.runtime import AppContext


def router(ctx: AppContext) -> APIRouter:
    r = APIRouter()
    svc = ctx.services.media

    @r.post("/api/requirements/{req_id}/media")
    async def upload(req_id: str, request: Request, name: str = "", mediaId: str = "") -> dict[str, object]:
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
        return Response(
            content=d.data,
            media_type=media_type,
            headers={"ETag": etag, "Cache-Control": "private, max-age=31536000, immutable"},
        )

    return r
