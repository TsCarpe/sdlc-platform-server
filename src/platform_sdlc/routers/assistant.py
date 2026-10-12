"""聊天助理端点（新增）：POST /api/assistant/chat（SSE 流式）+ 会话管理。

会话记忆存独立 sqlite（data/assistant.db），不进 platform.db（单写者模型不变）。
X-Token 说明：本端点为 POST /api/ 写面，启用 write-token 的部署需带 X-Token（与 Runner 同口径）。
SSE 事件：{type:"delta",text} … {type:"done"} / {type:"error",message}。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from agents import Runner
from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse
from openai.types.responses import ResponseTextDeltaEvent

from platform_sdlc.agent.assistant import (
    AssistantContext,
    SqliteChatSession,
    build_assistant,
    list_session_ids,
)
from platform_sdlc.common import ApiException
from platform_sdlc.routers.http import read_json
from platform_sdlc.runtime import AppContext

log = logging.getLogger(__name__)

_SAFE_SID = re.compile(r"[^A-Za-z0-9._-]")


def _sse(payload: dict[str, Any]) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


def _session_db(ctx: AppContext) -> Path:
    return ctx.paths.db.parent / "assistant.db"


def router(ctx: AppContext) -> APIRouter:
    r = APIRouter()
    _agent: list[Any] = [None]  # 惰性构建（首次调用时接线模型，避免无 agent 配置时启动失败）

    def agent() -> Any:
        if _agent[0] is None:
            _agent[0] = build_assistant(ctx.settings)
        return _agent[0]

    @r.post("/api/assistant/chat")
    async def chat(request: Request) -> StreamingResponse:
        body = await read_json(request, required=True)
        sid_raw = str(body.get("sessionId") or "")
        message = str(body.get("message") or "").strip()
        sid = _SAFE_SID.sub("", sid_raw)[:64]
        if not sid:
            raise ApiException(400, "缺少必填字段：sessionId")
        if not message:
            raise ApiException(400, "缺少必填字段：message")
        session = await asyncio.to_thread(SqliteChatSession, sid, _session_db(ctx))

        async def stream() -> AsyncIterator[str]:
            try:
                result = Runner.run_streamed(agent(), message,
                                             context=AssistantContext(services=ctx.services, repos=ctx.repos), session=session)
                async for event in result.stream_events():
                    if event.type == "raw_response_event" and isinstance(event.data, ResponseTextDeltaEvent):
                        yield _sse({"type": "delta", "text": event.data.delta})
                yield _sse({"type": "done"})
            except Exception as e:  # noqa: BLE001——流中异常以 error 事件收尾（前端可展示）；服务端同步留痕（AUDIT D7-6）
                log.warning("助理 SSE 流异常 sid=%s：%s: %s", sid, type(e).__name__, e)
                yield _sse({"type": "error", "message": f"{type(e).__name__}: {e}"})

        return StreamingResponse(stream(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @r.get("/api/assistant/sessions")
    async def sessions() -> list[str]:
        return await asyncio.to_thread(list_session_ids, _session_db(ctx))

    @r.delete("/api/assistant/sessions/{sid}")
    async def clear_session(sid: str) -> dict[str, Any]:
        session = SqliteChatSession(_SAFE_SID.sub("", sid)[:64], _session_db(ctx))
        await session.clear_session()
        return {"ok": True, "cleared": sid}

    return r
