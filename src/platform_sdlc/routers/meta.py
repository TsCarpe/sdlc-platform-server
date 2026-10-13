"""元信息端点：任务通道共享规则下发（AUDIT D1-5，批次7）——runner 启动拉取，
selfCheck 阈值与任务→草稿映射单一来源，双通道不再各自漂移。只读，无副作用。
v4-3（DESIGN-v4 §3）：携带 workspaceRoot（服务端 ResolvedPaths.workspace_root，两通道输出定位同源）。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from platform_sdlc.runtime import AppContext
from platform_sdlc.task_rules import task_rules_payload


def router(ctx: AppContext) -> APIRouter:
    r = APIRouter()

    @r.get("/api/meta/task-rules")
    async def task_rules() -> dict[str, Any]:
        return task_rules_payload(str(ctx.paths.workspace_root))  # type: ignore[no-any-return]

    return r
