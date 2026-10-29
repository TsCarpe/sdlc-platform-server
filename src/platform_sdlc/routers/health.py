"""健康检查（对应 HealthController：{ok, app, version}；version 键保持既有契约值）。

K8s 探针分工（EXEC-PLAN-K8s 阶段 1）：/api/health 保持静态（liveness/startup 用——DB 抖动
不引发重启风暴）；/api/health/ready 探 MySQL 连通性（readiness 用，DB 不可达即摘流不重启）。
"""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, HTTPException

from platform_sdlc.runtime import AppContext


def router(ctx: AppContext) -> APIRouter:
    r = APIRouter()

    @r.get("/api/health")
    async def health() -> dict[str, Any]:
        return {"ok": True, "app": "sdlc-platform", "version": "0.2.0"}

    @r.get("/api/health/ready")
    async def ready() -> dict[str, Any]:
        # 探测不取全局 RLock（Database.ping 池租借语义）；async 路由内同步 DB 调用走 to_thread
        try:
            await asyncio.to_thread(ctx.db.ping)
        except Exception as exc:
            raise HTTPException(status_code=503, detail=f"数据库未就绪：{exc}") from exc
        return {"ok": True, "db": "up"}

    return r
