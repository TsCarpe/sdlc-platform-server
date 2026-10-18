"""用例域端点（对应 CaseController）。路由层服务调用一律 asyncio.to_thread（不冻结事件循环）。"""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Request

from platform_sdlc.routers.http import read_json
from platform_sdlc.runtime import AppContext


def router(ctx: AppContext) -> APIRouter:
    r = APIRouter()
    svc = ctx.services.cases

    @r.post("/api/cases")
    async def sync_upsert(request: Request) -> dict[str, Any]:
        """skill 批量 upsert（reqId 必填校验在服务层）。"""
        body = await read_json(request, required=True)
        return await asyncio.to_thread(svc.sync_upsert, None, body)

    @r.post("/api/cases/form")
    async def form_create(request: Request) -> dict[str, Any]:
        body = await read_json(request, required=True)
        return await asyncio.to_thread(svc.form_create, None, body)

    @r.get("/api/cases")
    async def list_cases(iter: str | None = None, reqId: str | None = None, status: str | None = None,
                         tool: str | None = None, q: str | None = None) -> list[dict[str, Any]]:
        return await asyncio.to_thread(svc.list, iter, reqId, status, tool, q)

    @r.get("/api/cases/{req_id}/{case_id}")
    async def detail(req_id: str, case_id: str) -> dict[str, Any]:
        return await asyncio.to_thread(svc.detail, req_id, case_id)

    @r.patch("/api/cases/{req_id}/{case_id}")
    async def update(req_id: str, case_id: str, request: Request) -> dict[str, Any]:
        body = await read_json(request, required=True)
        return await asyncio.to_thread(svc.form_update, req_id, case_id, body)

    @r.delete("/api/cases/{req_id}/{case_id}")
    async def delete(req_id: str, case_id: str) -> dict[str, Any]:
        """表单用例删除（P1-7）；sdlc-test 来源 409（只读+评论）。"""
        return await asyncio.to_thread(svc.form_delete, req_id, case_id)

    @r.post("/api/cases/{req_id}/{case_id}/comments")
    async def comment(req_id: str, case_id: str, request: Request) -> dict[str, Any]:
        body = await read_json(request, required=True)
        return await asyncio.to_thread(svc.add_comment, req_id, case_id, body)

    return r
