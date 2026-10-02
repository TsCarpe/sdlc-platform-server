"""设计 lane 端点（对应 DesignController，DESIGN-v2.4 D34）。"""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Request

from platform_sdlc.common import OkResult
from platform_sdlc.routers.http import read_json
from platform_sdlc.runtime import AppContext


def router(ctx: AppContext) -> APIRouter:
    r = APIRouter()
    svc = ctx.services.designs

    @r.get("/api/requirements/{req_id}/design")
    async def view(req_id: str) -> dict[str, Any]:
        return await asyncio.to_thread(svc.view, req_id)

    @r.put("/api/requirements/{req_id}/target-repos")
    async def set_target_repos(req_id: str, request: Request) -> dict[str, Any]:
        # 多目标仓全量替换（D39）：{repos:[{role,kind,ref,branch}]}，首仓镜像 target_repo
        body = await read_json(request, required=True)
        await asyncio.to_thread(svc.set_target_repos, req_id, body)
        return OkResult().model_dump()

    @r.post("/api/requirements/{req_id}/design/skip")
    async def skip(req_id: str, request: Request) -> dict[str, Any]:
        # C 级轻量跳过声明（留痕可审计；声明后 design_gen 拒绝，门禁以自查档执行）
        body = await read_json(request)
        await asyncio.to_thread(svc.skip, req_id, body)
        return await asyncio.to_thread(svc.view, req_id)

    return r
