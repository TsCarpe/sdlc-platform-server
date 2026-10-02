"""门禁域端点（对应 GateController，DESIGN-v2.4 D35）。六态为读面推导，落库仅权威两值。"""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Request

from platform_sdlc.routers.http import read_json
from platform_sdlc.runtime import AppContext


def router(ctx: AppContext) -> APIRouter:
    r = APIRouter()
    svc = ctx.services.gate

    @r.get("/api/requirements/{req_id}/gate")
    async def view(req_id: str) -> dict[str, Any]:
        return await asyncio.to_thread(svc.view, req_id)

    @r.post("/api/requirements/{req_id}/gate/open")
    async def open(req_id: str, request: Request) -> dict[str, Any]:
        # 开闸（GR-2 三条件机械校验不过 409 带明细；通过 → 建轮 + 排 gate_review）
        body = await read_json(request)
        return await asyncio.to_thread(svc.open, req_id, body)

    @r.post("/api/requirements/{req_id}/gate/rounds/{round_id}/ingest")
    async def ingest(req_id: str, round_id: int, request: Request) -> dict[str, Any]:
        # issues 草稿确认入库（md×json 机械校验，全绿才落 gate_issue）
        body = await read_json(request)
        return await asyncio.to_thread(svc.ingest, req_id, round_id, body)

    @r.get("/api/requirements/{req_id}/gate/change-status")
    async def change_status(req_id: str) -> dict[str, Any]:
        # GR-3 变更检测（内容比对基线；仅「已放行后」窗口出横幅，评审中变更走落改）
        return await asyncio.to_thread(svc.change_status, req_id)

    @r.post("/api/requirements/{req_id}/gate/reopen")
    async def reopen(req_id: str, request: Request) -> dict[str, Any]:
        # GR-3 复审：放行后检测到实质变更，人确认开新一轮（旧轮只读归档）
        body = await read_json(request)
        return await asyncio.to_thread(svc.reopen, req_id, body)

    @r.post("/api/gate/issues/{issue_id}/verdict")
    async def verdict(issue_id: int, request: Request) -> dict[str, Any]:
        # 裁决三处置（按裁决单元批量落）
        body = await read_json(request, required=True)
        return await asyncio.to_thread(svc.verdict, issue_id, body)

    @r.post("/api/gate/issues/{issue_id}/revision")
    async def revision(issue_id: int, request: Request) -> dict[str, Any]:
        body = await read_json(request, required=True)
        # 修订工作项推进（落改 → 已执行，GR-4 放行前提）
        return await asyncio.to_thread(svc.revision, issue_id, str(body.get("revLocation") or ""), str(body.get("revStatus") or ""))

    @r.post("/api/gate/rounds/{round_id}/release")
    async def release(round_id: int, request: Request) -> dict[str, Any]:
        # 放行（GR-4 全绿 + 关卡互认）
        body = await read_json(request)
        return await asyncio.to_thread(svc.release, round_id, body)

    return r
