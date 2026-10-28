"""路由注册入口：每个控制器一个模块，工厂函数注入 AppContext。"""

from __future__ import annotations

from fastapi import FastAPI

from platform_sdlc.runtime import AppContext


def register_routers(app: FastAPI, ctx: AppContext) -> None:
    from platform_sdlc.routers import (
        agent_admin,
        assistant,
        cases,
        changes,
        design,
        gate,
        health,
        iterations,
        media,
        requirements,
        tasks,
    )

    app.include_router(health.router)
    app.include_router(requirements.router(ctx))
    app.include_router(changes.router(ctx))
    app.include_router(cases.router(ctx))
    app.include_router(tasks.router(ctx))
    app.include_router(media.router(ctx))
    app.include_router(iterations.router(ctx))
    app.include_router(design.router(ctx))
    app.include_router(gate.router(ctx))
    app.include_router(agent_admin.router(ctx))
    app.include_router(assistant.router(ctx))
