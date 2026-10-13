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

    @r.post("/api/requirements/{req_id}/changes/{change_id}/confirm")
    async def confirm(req_id: str, change_id: int, request: Request) -> dict[str, Any]:
        """confirm = 一个事务（§5.5）：items 应用 + 乐观锁校验 + impact 冻结 + content_rev+1 +
        published→changed（表外通道）+ 受影响澄清重开。body: {confirmedBy, impact?}。"""
        body = await read_json(request, required=True)
        out = await asyncio.to_thread(svc.confirm, req_id, change_id, body)
        # L2：影响面非空自动排增量质检（编排失败不连累确认；任务类型 v4-14 注册）
        await asyncio.to_thread(ctx.services.orchestration.on_change_confirmed,
                                req_id, change_id, out.get("impact"))
        return out

    @r.post("/api/requirements/{req_id}/changes/{change_id}/abort")
    async def abort(req_id: str, change_id: int, request: Request) -> dict[str, Any]:
        """放弃变更（§5.6）：全量逆向回滚（edit 逆/insert 逆软删/delete 逆复活/move 逆恢复 seq/
        澄清 pending-only 恢复）+ aborted + changed→published；content_rev 不回退。LIFO 仅最新单。"""
        body = await read_json(request, required=True)
        return await asyncio.to_thread(svc.abort, req_id, change_id, body)

    @r.get("/api/requirements/{req_id}/changes/{change_id}/impact")
    async def preview_impact(req_id: str, change_id: int) -> dict[str, Any]:
        """影响面预览（机械 join 现算不冻结，§5.4）：受影响块集（edit/move 直接 ∪ delete 子孙递归
        CTE）→ 四类清单（澄清项/FR/digest 行/关联用例）+ 章节（heading block_id 表示，禁 seq）。"""
        return await asyncio.to_thread(svc.preview_impact, req_id, change_id)

    return r
