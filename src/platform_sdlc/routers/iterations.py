"""迭代域端点（对应 IterationController，DESIGN-v2.4 D32/D33）。

迭代名可含 `/` 等字符，detail/delete 走 query 参（路径段会吞斜杠/被容器拒 %2F）。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request

from platform_sdlc.common import ApiException, OkResult
from platform_sdlc.routers.http import read_json
from platform_sdlc.runtime import AppContext


def router(ctx: AppContext) -> APIRouter:
    r = APIRouter()
    svc = ctx.services.iterations

    def require_name(name: str | None) -> str:
        if name is None or not name.strip():
            raise ApiException(400, "缺少迭代名（?name=）")
        return name.strip()

    @r.get("/api/iterations")
    async def list_all() -> list[dict[str, Any]]:
        out = []
        for it in svc.list():
            out.append({
                "name": it.name, "note": it.note, "createdBy": it.created_by, "createdAt": it.created_at,
                "reqId": it.req_id, "reqName": it.req_name, "reqStatus": it.req_status,
                "riskTier": it.risk_tier, "submittedBy": it.submitted_by,
                "totalQ": it.total_q, "answeredQ": it.answered_q, "caseCount": it.case_count,
                "designPhase": it.design_phase, "gateStage": it.gate_stage,
            })
        return out

    @r.post("/api/iterations")
    async def create(request: Request) -> dict[str, Any]:
        return {"name": svc.create(await read_json(request, required=True))}

    @r.get("/api/iterations/detail")
    async def detail(name: str | None = None) -> dict[str, Any]:
        it = svc.get(require_name(name))
        return {
            "name": it.name, "note": it.note, "createdBy": it.created_by, "createdAt": it.created_at,
            "reqId": it.req_id, "reqName": it.req_name, "reqStatus": it.req_status,
            "riskTier": it.risk_tier, "submittedBy": it.submitted_by,
            "totalQ": it.total_q, "answeredQ": it.answered_q, "caseCount": it.case_count,
            "designPhase": it.design_phase, "gateStage": it.gate_stage,
        }

    @r.delete("/api/iterations/detail")
    async def delete(name: str | None = None) -> dict[str, Any]:
        svc.delete(require_name(name))
        return OkResult().model_dump()

    return r
