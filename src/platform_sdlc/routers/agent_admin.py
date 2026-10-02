"""进程内 agent 运行状态与回归暂停开关（对应 AgentAdminController）。

ctx.agent 在 lifespan 阶段才挂载（agent_enabled 时启动 loop），处理器内惰性解引用；
agent_enabled=false 时返回 stub 形状（verify 脚本的 pause/resume 幂等可用）。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from platform_sdlc.runtime import AppContext

_STUB = {"enabled": False, "paused": False, "running": False}


def router(ctx: AppContext) -> APIRouter:
    r = APIRouter()

    @r.get("/api/agent/status")
    async def status() -> dict[str, Any]:
        return ctx.agent.status() if ctx.agent is not None else {**_STUB, "enabled": ctx.settings.agent_enabled}

    @r.post("/api/agent/pause")
    async def pause() -> dict[str, Any]:
        return ctx.agent.pause() if ctx.agent is not None else {**_STUB, "enabled": ctx.settings.agent_enabled}

    @r.post("/api/agent/resume")
    async def resume() -> dict[str, Any]:
        return ctx.agent.resume() if ctx.agent is not None else {**_STUB, "enabled": ctx.settings.agent_enabled}

    return r
