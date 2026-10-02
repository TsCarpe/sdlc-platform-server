"""健康检查（对应 HealthController：{ok, app, version}；version 键保持既有契约值）。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter

router = APIRouter()


@router.get("/api/health")
async def health() -> dict[str, Any]:
    return {"ok": True, "app": "sdlc-platform", "version": "0.2.0"}
