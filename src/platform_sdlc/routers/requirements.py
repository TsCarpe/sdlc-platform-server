"""需求域端点（对应 RequirementController）。路由层服务调用一律 asyncio.to_thread（sqlite/文件
I/O 不冻结事件循环——进程内 agent 轮询与 SSE 流与本服务共享同一循环）。"""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Request

from platform_sdlc.common import ApiException, OkResult
from platform_sdlc.routers.http import read_json
from platform_sdlc.runtime import AppContext


def router(ctx: AppContext) -> APIRouter:
    r = APIRouter()
    svc = ctx.services.requirements
    orch = ctx.services.orchestration

    @r.post("/api/requirements")
    async def create(request: Request) -> dict[str, Any]:
        body = await read_json(request, required=True)
        created = await asyncio.to_thread(svc.create, body)
        # v4-4（DESIGN-v4 §3）：建卡成功 → L2 自动排 source_import（只一次；失败不连累建卡）
        await asyncio.to_thread(orch.on_created, created.id)
        return {"id": created.id, "status": "submitted"}

    @r.get("/api/requirements")
    async def list_all(iter: str | None = None, status: str | None = None) -> list[dict[str, Any]]:
        return await asyncio.to_thread(svc.list_all, iter, status)

    @r.get("/api/requirements/{req_id}")
    async def detail(req_id: str) -> dict[str, Any]:
        return await asyncio.to_thread(svc.detail, req_id)

    @r.get("/api/requirements/{req_id}/doc.json")
    async def doc_json(req_id: str) -> dict[str, Any]:
        """原文同形状导出（DESIGN-v4 §2.3/§12 一期：curl -o 直落供质检管线；块表权威，空表回退 doc artifact）。"""
        def load() -> dict[str, Any]:
            svc.require(req_id)  # 404 守卫（软删根过滤）
            out = ctx.services.blocks.doc_export(req_id)
            if out is None:
                raise ApiException(404, f"原文未导入：{req_id}（无块表数据，先完成 source_import）")
            return out

        return await asyncio.to_thread(load)

    @r.get("/api/requirements/{req_id}/comments.json")
    async def comments_json(req_id: str) -> list[dict[str, Any]]:
        """评论兼容导出（resolve_anchors --comments 契约：数组 + comment_id，按序号 → comment-id 映射）。"""
        def load() -> list[dict[str, Any]]:
            svc.require(req_id)
            return ctx.services.blocks.comments_payload(req_id)

        return await asyncio.to_thread(load)

    @r.put("/api/requirements/{req_id}/blocks")
    async def put_blocks(req_id: str, request: Request) -> dict[str, Any]:
        """窗口 1 块级直接编辑（DESIGN-v4 §5.3，EXEC v4-7）：全量块数组 → 服务端 diff（LIS 判 move）
        → 事务应用（version+1 留痕 / insert 发 blk-<ulid> / delete 软删 / 整体重排 seq）。"""
        body = await read_json(request, required=True)
        return await asyncio.to_thread(ctx.services.blocks.apply_full_blocks, req_id, body)

    @r.post("/api/requirements/{req_id}/blocks/diff")
    async def blocks_diff_preview(req_id: str, request: Request) -> dict[str, Any]:
        """diff 预览（dry-run 不落库）：保存前四态预览（edit/insert/delete/move）——三期变更单 diff
        视图复用同形态；GET 载不动全量块数组，预览承载面为 POST。"""
        body = await read_json(request, required=True)
        return await asyncio.to_thread(ctx.services.blocks.preview_diff, req_id, body)

    @r.post("/api/requirements/{req_id}/artifacts")
    async def artifacts(req_id: str, request: Request) -> dict[str, Any]:
        body = await read_json(request, required=True)
        return await asyncio.to_thread(svc.push_artifacts, req_id, body)

    @r.get("/api/requirements/{req_id}/answers")
    async def answers(req_id: str) -> list[dict[str, Any]]:
        return await asyncio.to_thread(svc.answers, req_id)

    @r.put("/api/requirements/{req_id}/answers/{seq}")
    async def answer(req_id: str, seq: str, request: Request) -> dict[str, Any]:
        # D21 处置通道：body 可选携带 status/note，缺省=作答语义
        body = await read_json(request, required=True)
        await asyncio.to_thread(svc.set_answer, req_id, seq, body)
        await asyncio.to_thread(orch.on_answered, req_id)  # L2：全部答完自动排 standard_doc（编排失败不连累作答）
        return OkResult().model_dump()

    @r.post("/api/requirements/{req_id}/publish")
    async def publish(req_id: str, request: Request) -> dict[str, Any]:
        body = await read_json(request, required=True)
        out = await asyncio.to_thread(svc.publish, req_id, body)
        await asyncio.to_thread(orch.on_published, req_id)  # L2：发布后自动排 case_gen（编排失败不连累发布）
        return out

    @r.post("/api/requirements/{req_id}/status")
    async def set_status(req_id: str, request: Request) -> dict[str, Any]:
        body = await read_json(request, required=True)
        await asyncio.to_thread(svc.set_status, req_id, body)
        return OkResult().model_dump()

    @r.delete("/api/requirements/{req_id}")
    async def delete(req_id: str) -> dict[str, Any]:
        return await asyncio.to_thread(svc.delete, req_id)

    @r.get("/api/fr-points")
    async def fr_points(req_id: str) -> dict[str, Any]:
        return await asyncio.to_thread(svc.fr_points, req_id)

    return r
