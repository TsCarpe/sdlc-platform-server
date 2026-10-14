"""任务队列与草稿端点（对应 TaskController）。

写端点统一 /api 前缀（X-Token 写保护覆盖）；GET /api/tasks/next 与 done/failed 供 Runner 调用。
"""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Request, Response

from platform_sdlc.routers.http import read_json
from platform_sdlc.runtime import AppContext
from platform_sdlc.schemas import DraftOut, TaskOut, wire


def router(ctx: AppContext) -> APIRouter:
    r = APIRouter()
    svc = ctx.services.tasks

    @r.post("/api/requirements/{req_id}/tasks", response_model=TaskOut)
    async def create(req_id: str, request: Request) -> dict[str, Any]:
        body = await read_json(request, required=True)
        return wire(await asyncio.to_thread(svc.create, req_id, body))

    @r.get("/api/tasks", response_model=list[TaskOut])
    async def list_tasks(req_id: str | None = None, status: str | None = None,
                         limit: int = 100) -> list[dict[str, Any]]:
        return [wire(t) for t in await asyncio.to_thread(svc.list, req_id, status, limit)]

    @r.post("/api/tasks/next")
    async def next_task(runner: str = "runner") -> Response:
        """Runner 原子领取；队列空返回 204 无 body。
        POST：领取是写操作，纳入 X-Token 写保护（原 GET 形态绕过令牌面——AUDIT D4-1）。"""
        t = await asyncio.to_thread(svc.claim, runner)
        if t is None:
            return Response(status_code=204)
        import json

        return Response(content=json.dumps(wire(t), ensure_ascii=False), media_type="application/json")

    @r.post("/api/tasks/recover-stale")
    async def recover_stale(runner: str = "") -> dict[str, Any]:
        """执行器启动恢复：超时 running 置 failed。runner 非空时只清该执行器名下任务
        （双通道各恢复自己，防跨通道互杀——AUDIT D1-3；超时阈值用服务端单一配置）。"""
        n = await asyncio.to_thread(svc.expire_stale, ctx.settings.agent_timeout_min, runner or None)
        return {"ok": True, "expired": n}

    @r.post("/api/tasks/{task_id}/done", response_model=TaskOut)
    async def done(task_id: int, request: Request) -> dict[str, Any]:
        body = await read_json(request)
        return wire(await asyncio.to_thread(svc.finish, task_id, True, body))

    @r.post("/api/tasks/{task_id}/failed", response_model=TaskOut)
    async def failed(task_id: int, request: Request) -> dict[str, Any]:
        body = await read_json(request)
        return wire(await asyncio.to_thread(svc.finish, task_id, False, body))

    @r.post("/api/tasks/{task_id}/retry", response_model=TaskOut)
    async def retry(task_id: int) -> dict[str, Any]:
        return wire(await asyncio.to_thread(svc.retry, task_id))

    @r.post("/api/tasks/{task_id}/cancel", response_model=TaskOut)
    async def cancel(task_id: int) -> dict[str, Any]:
        return wire(await asyncio.to_thread(svc.cancel, task_id))

    # ---------- 草稿 ----------

    @r.put("/api/requirements/{req_id}/drafts/{type}", response_model=DraftOut)
    async def put_draft(req_id: str, type: str, request: Request) -> dict[str, Any]:
        body = await read_json(request, required=True)
        return wire(await asyncio.to_thread(svc.put_draft, req_id, type, body))

    @r.get("/api/requirements/{req_id}/drafts", response_model=list[DraftOut])
    async def drafts(req_id: str) -> list[dict[str, Any]]:
        return [wire(d) for d in await asyncio.to_thread(svc.list_drafts, req_id)]

    @r.get("/api/requirements/{req_id}/drafts/{type}", response_model=DraftOut)
    async def draft(req_id: str, type: str) -> dict[str, Any]:
        return wire(await asyncio.to_thread(svc.get_draft, req_id, type))

    @r.delete("/api/requirements/{req_id}/drafts/{type}")
    async def delete_draft(req_id: str, type: str) -> dict[str, Any]:
        return await asyncio.to_thread(svc.delete_draft, req_id, type)

    @r.post("/api/requirements/{req_id}/drafts/{type}/approve")
    async def approve(req_id: str, type: str, request: Request) -> dict[str, Any]:
        # body 可选携带 by（design 定稿人；digest/cases 采纳不记名，历史契约不变）
        body = await read_json(request)
        out = await asyncio.to_thread(svc.approve, req_id, type, str(body.get("by") or ""))
        if type == "incremental_bundle":  # v4-14（§8）：合并采纳后 changed+澄清全处置 → 排重合成
            await asyncio.to_thread(ctx.services.orchestration.on_merged, req_id)
        return out

    return r
