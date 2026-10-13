"""v4.11 变更单端点（DESIGN-v4 §5.2/§5.3/§5.4）：draft CRUD + 影响面预览。

confirm（§5.5）/ 放弃（§5.6）通道随 v4-12/13 落地——本批只到 draft 数据面。
路径挂需求域下（/api/requirements/{req_id}/changes…），领域独立成模块（三期大域）。
"""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Request

from platform_sdlc.routers.http import read_json
from platform_sdlc.runtime import AppContext


def router(ctx: AppContext) -> APIRouter:
    r = APIRouter()
    svc = ctx.services.changes

    @r.post("/api/requirements/{req_id}/changes")
    async def create(req_id: str, request: Request) -> dict[str, Any]:
        """建 draft：{title, detail, proposedBy, blocks}——全量块数组服务端 diff 出 items
        （edit/insert/delete/move 四态，LIS 判 move；insert 草稿期即有平台 ID blk-）。"""
        body = await read_json(request, required=True)
        return await asyncio.to_thread(svc.create, req_id, body)

    @r.get("/api/requirements/{req_id}/changes")
    async def list_changes(req_id: str) -> list[dict[str, Any]]:
        return await asyncio.to_thread(svc.list, req_id)

    @r.get("/api/requirements/{req_id}/changes/{change_id}")
    async def detail(req_id: str, change_id: int) -> dict[str, Any]:
        return await asyncio.to_thread(svc.detail, req_id, change_id)

    @r.put("/api/requirements/{req_id}/changes/{change_id}")
    async def save(req_id: str, change_id: int, request: Request) -> dict[str, Any]:
        """反复保存：items 全量重算替换（draft 期不冻结）；仅 draft 可存。"""
        body = await read_json(request, required=True)
        return await asyncio.to_thread(svc.save, req_id, change_id, body)

    @r.delete("/api/requirements/{req_id}/changes/{change_id}")
    async def delete(req_id: str, change_id: int) -> dict[str, Any]:
        """draft 可直接删（未碰块表零代价）；confirmed 不可删（不作废，§5.6）。"""
        return await asyncio.to_thread(svc.delete, req_id, change_id)

    @r.get("/api/requirements/{req_id}/changes/{change_id}/impact")
    async def preview_impact(req_id: str, change_id: int) -> dict[str, Any]:
        """影响面预览（机械 join 现算不冻结，§5.4）：受影响块集（edit/move 直接 ∪ delete 子孙递归
        CTE）→ 四类清单（澄清项/FR/digest 行/关联用例）+ 章节（heading block_id 表示，禁 seq）。"""
        return await asyncio.to_thread(svc.preview_impact, req_id, change_id)

    return r
