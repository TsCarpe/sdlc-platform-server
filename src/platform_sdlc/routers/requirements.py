"""需求域端点（对应 RequirementController）。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request

from platform_sdlc.common import OkResult
from platform_sdlc.routers.http import read_json
from platform_sdlc.runtime import AppContext


def router(ctx: AppContext) -> APIRouter:
    r = APIRouter()
    svc = ctx.services.requirements
    orch = ctx.services.orchestration

    @r.post("/api/requirements")
    async def create(request: Request) -> dict[str, Any]:
        body = await read_json(request, required=True)
        created = svc.create(body)
        return {"id": created.id, "status": "submitted"}

    @r.get("/api/requirements")
    async def list_all(iter: str | None = None, status: str | None = None) -> list[dict[str, Any]]:
        return svc.list_all(iter, status)

    @r.get("/api/requirements/{req_id}")
    async def detail(req_id: str) -> dict[str, Any]:
        return svc.detail(req_id)

    @r.post("/api/requirements/{req_id}/artifacts")
    async def artifacts(req_id: str, request: Request) -> dict[str, Any]:
        return svc.push_artifacts(req_id, await read_json(request, required=True))

    @r.get("/api/requirements/{req_id}/answers")
    async def answers(req_id: str) -> list[dict[str, Any]]:
        return svc.answers(req_id)

    @r.put("/api/requirements/{req_id}/answers/{seq}")
    async def answer(req_id: str, seq: str, request: Request) -> dict[str, Any]:
        # D21 处置通道：body 可选携带 status/note，缺省=作答语义
        body = await read_json(request, required=True)
        svc.set_answer(req_id, seq, body)
        orch.on_answered(req_id)  # L2：全部答完自动排 standard_doc（编排失败不连累作答）
        return OkResult().model_dump()

    @r.post("/api/requirements/{req_id}/publish")
    async def publish(req_id: str, request: Request) -> dict[str, Any]:
        out = svc.publish(req_id, await read_json(request, required=True))
        orch.on_published(req_id)  # L2：发布后自动排 case_gen（编排失败不连累发布）
        return out

    @r.post("/api/requirements/{req_id}/status")
    async def set_status(req_id: str, request: Request) -> dict[str, Any]:
        svc.set_status(req_id, await read_json(request, required=True))
        return OkResult().model_dump()

    @r.delete("/api/requirements/{req_id}")
    async def delete(req_id: str) -> dict[str, Any]:
        return svc.delete(req_id)

    @r.get("/api/fr-points")
    async def fr_points(req_id: str) -> dict[str, Any]:
        return svc.fr_points(req_id)

    return r
