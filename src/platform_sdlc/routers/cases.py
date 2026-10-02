"""用例域端点（对应 CaseController）。"""

from __future__ import annotations

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
        return svc.sync_upsert(None, await read_json(request, required=True))

    @r.post("/api/cases/form")
    async def form_create(request: Request) -> dict[str, Any]:
        return svc.form_create(None, await read_json(request, required=True))

    @r.get("/api/cases")
    async def list_cases(iter: str | None = None, reqId: str | None = None, status: str | None = None,
                         tool: str | None = None, q: str | None = None) -> list[dict[str, Any]]:
        return svc.list(iter, reqId, status, tool, q)

    @r.get("/api/cases/{req_id}/{case_id}")
    async def detail(req_id: str, case_id: str) -> dict[str, Any]:
        return svc.detail(req_id, case_id)

    @r.patch("/api/cases/{req_id}/{case_id}")
    async def update(req_id: str, case_id: str, request: Request) -> dict[str, Any]:
        return svc.form_update(req_id, case_id, await read_json(request, required=True))

    @r.post("/api/cases/{req_id}/{case_id}/comments")
    async def comment(req_id: str, case_id: str, request: Request) -> dict[str, Any]:
        return svc.add_comment(req_id, case_id, await read_json(request, required=True))

    return r
