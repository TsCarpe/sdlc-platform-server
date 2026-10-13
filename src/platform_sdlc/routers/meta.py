"""元信息端点：任务通道共享规则下发（AUDIT D1-5，批次7）——runner 启动拉取，
selfCheck 阈值与任务→草稿映射单一来源，双通道不再各自漂移。只读，无副作用。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from platform_sdlc.task_rules import task_rules_payload


def router(_: Any) -> APIRouter:
    r = APIRouter()

    @r.get("/api/meta/task-rules")
    async def task_rules() -> dict[str, Any]:
        return task_rules_payload()  # type: ignore[no-any-return]

    return r
