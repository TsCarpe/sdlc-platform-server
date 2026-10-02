"""查询类聊天助理（新增能力，openai-agents）。

- 只读工具面：跨需求查询（检索/详情/澄清/用例/设计/门禁/迭代）——正式写入服务不进 context；
- 会话记忆：自实现 Session 协议，独立 sqlite 文件（不进 platform.db，保单写者模型）；
- 输出：Runner.run_streamed + SSE 流式到前端。
"""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agents import Agent, RunContextWrapper, function_tool, set_default_openai_api, set_default_openai_client, set_tracing_disabled
from openai import AsyncOpenAI

from platform_sdlc.config import Settings
from platform_sdlc.services import Services

_INSTRUCTIONS = """你是 SDLC 产物协作平台的查询助理，面向 PM/测试/开发答疑。平台数据以只读工具提供。

纪律（违反即失职）：
1. 只回答能从工具结果支撑的内容；工具查不到就如实说查不到，禁止编造需求编号、状态或数字。
2. 回答用简体中文，简洁为主；引用需求时带上编号（如 REQ-2026-001）与状态；列清单时给条数。
3. 引用记号（需求.FR-xx / 体检.Pxx / 需求.Axx / Pxx）是平台既有锚点，照原文引用可帮助用户在页面内跳转。
4. 数据有明显矛盾或为空时，指出你看到的原始值，不做推断性补全。"""


@dataclass
class AssistantContext:
    services: Services


# ---------- 只读工具面（跨需求；正式写入通道不存在） ----------


async def _read(async_fn):  # noqa: ANN001——薄包装：阻塞查询进线程池
    return await asyncio.to_thread(async_fn)


def _tool_err(e: Exception) -> str:
    return json.dumps({"error": str(e)}, ensure_ascii=False)


@function_tool
async def search_requirements(wrapper: RunContextWrapper[AssistantContext], q: str = "",
                              iter: str = "", status: str = "") -> str:
    """检索需求卡列表（按名称/编号模糊匹配，可按迭代与状态过滤）。返回编号、名称、状态、澄清进度、用例数。

    Args:
        q: 关键字（匹配需求名称或编号，可空）
        iter: 迭代名精确过滤（可空）
        status: 状态精确过滤（submitted|reviewing|clarifying|published，可空）
    """
    def impl() -> str:
        rows = wrapper.context.services.requirements.list_all(iter or None, status or None)
        if q.strip():
            needle = q.strip().lower()
            rows = [r for r in rows if needle in r["name"].lower() or needle in r["id"].lower()]
        rows = rows[:20]
        return json.dumps([{"id": r["id"], "name": r["name"], "iter": r["iter"], "status": r["status"],
                            "answeredQ": r["answeredQ"], "totalQ": r["totalQ"], "caseCount": r["caseCount"]}
                           for r in rows], ensure_ascii=False)

    return await _read(impl)


@function_tool
async def get_requirement_summary(wrapper: RunContextWrapper[AssistantContext], req_id: str) -> str:
    """读取单个需求的概要：状态、风险分级、澄清处置进度、需求点数、用例数、质检星级、是否已发布/设计/门禁阶段。

    Args:
        req_id: 需求编号，如 REQ-2026-001
    """
    def impl() -> str:
        s = wrapper.context.services
        r = s.requirements.require(req_id)
        counts = s.repos.clarify_item.counts(req_id)
        design = s.designs.view(req_id)
        gate = s.gate.view(req_id)
        return json.dumps({
            "id": r.id, "name": r.name, "status": r.status, "riskTier": r.risk_tier,
            "clarify": f"{counts['answered']}/{counts['total']}",
            "frCount": len(s.repos.fr_point.list_by_req(req_id)),
            "caseCount": s.repos.test_case.count_by_req(req_id),
            "designPhase": design.get("phase"), "gatePhase": gate.get("phase"),
        }, ensure_ascii=False)

    try:
        return await _read(impl)
    except Exception as e:  # noqa: BLE001
        return _tool_err(e)


@function_tool
async def list_clarify_items(wrapper: RunContextWrapper[AssistantContext], req_id: str, unanswered_only: bool = False) -> str:
    """列出需求的澄清问题（含 PM 答复与处置状态）。

    Args:
        req_id: 需求编号
        unanswered_only: 只看待处置（true/false，默认 false）
    """
    def impl() -> str:
        items = wrapper.context.services.requirements.answers(req_id)
        if unanswered_only:
            items = [i for i in items if i["status"] in ("", "pending")]
        return json.dumps(items[:30], ensure_ascii=False)

    try:
        return await _read(impl)
    except Exception as e:  # noqa: BLE001
        return _tool_err(e)


@function_tool
async def list_cases(wrapper: RunContextWrapper[AssistantContext], req_id: str, q: str = "") -> str:
    """列出需求的测试用例（编号、标题、优先级、状态、追溯 points/verifyRefs）。

    Args:
        req_id: 需求编号
        q: 标题模糊过滤（可空）
    """
    def impl() -> str:
        rows = wrapper.context.services.cases.list(None, req_id, None, None, q or None)
        return json.dumps([{k: r[k] for k in ("id", "title", "priority", "status", "points", "verifyRefs", "tool")}
                           for r in rows[:30]], ensure_ascii=False)

    try:
        return await _read(impl)
    except Exception as e:  # noqa: BLE001
        return _tool_err(e)


@function_tool
async def get_gate_status(wrapper: RunContextWrapper[AssistantContext], req_id: str) -> str:
    """读取需求门禁视图：六态阶段、评审轮、开闸三条件、裁决单元进度。

    Args:
        req_id: 需求编号
    """
    def impl() -> str:
        v = wrapper.context.services.gate.view(req_id)
        rounds = v.get("rounds") or []
        units = v.get("units") or []
        judged = sum(1 for u in units if u.get("verdict"))
        return json.dumps({"phase": v.get("phase"), "tier": v.get("tier"),
                           "checks": v.get("checks"), "rounds": rounds,
                           "units": f"{judged}/{len(units)} 已裁决"}, ensure_ascii=False)

    try:
        return await _read(impl)
    except Exception as e:  # noqa: BLE001
        return _tool_err(e)


@function_tool
async def get_design_status(wrapper: RunContextWrapper[AssistantContext], req_id: str) -> str:
    """读取需求设计 lane 视图：阶段（none/draft/final/skipped）、目标仓、活跃任务。

    Args:
        req_id: 需求编号
    """
    def impl() -> str:
        return json.dumps(wrapper.context.services.designs.view(req_id), ensure_ascii=False)

    try:
        return await _read(impl)
    except Exception as e:  # noqa: BLE001
        return _tool_err(e)


@function_tool
async def list_iterations(wrapper: RunContextWrapper[AssistantContext]) -> str:
    """列出全部迭代及绑定需求摘要（含设计/门禁阶段推导）。"""
    def impl() -> str:
        return json.dumps([{
            "name": it.name, "reqId": it.req_id, "reqName": it.req_name, "reqStatus": it.req_status,
            "designPhase": it.design_phase, "gateStage": it.gate_stage,
        } for it in wrapper.context.services.iterations.list()], ensure_ascii=False)

    return await _read(impl)


ASSISTANT_TOOLS = [search_requirements, get_requirement_summary, list_clarify_items,
                   list_cases, get_gate_status, get_design_status, list_iterations]


class SqliteChatSession:
    """Session 协议的 sqlite 实现：独立文件，多轮记忆（对齐 SQLiteSession 接口面）。"""

    def __init__(self, session_id: str, db_path: Path) -> None:
        self.session_id = session_id
        import sqlite3

        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS chat_items ("
            "session_id TEXT NOT NULL, idx INTEGER NOT NULL, data TEXT NOT NULL,"
            " PRIMARY KEY (session_id, idx))")

    async def get_items(self, limit: int | None = None) -> list[Any]:
        def impl() -> list[Any]:
            rows = self._conn.execute(
                "SELECT data FROM chat_items WHERE session_id=? ORDER BY idx DESC LIMIT ?",
                (self.session_id, limit if limit is not None else -1)).fetchall()
            return [json.loads(r[0]) for r in reversed(rows)]

        return await asyncio.to_thread(impl)

    async def add_items(self, items: list[Any]) -> None:
        def impl() -> None:
            cur = self._conn.execute("SELECT COALESCE(MAX(idx), -1) FROM chat_items WHERE session_id=?",
                                     (self.session_id,))
            idx = int(cur.fetchone()[0]) + 1
            for item in items:
                self._conn.execute("INSERT INTO chat_items(session_id, idx, data) VALUES(?,?,?)",
                                   (self.session_id, idx, json.dumps(item, ensure_ascii=False)))
                idx += 1
            self._conn.commit()

        await asyncio.to_thread(impl)

    async def pop_item(self) -> Any:
        def impl() -> Any:
            row = self._conn.execute(
                "SELECT idx, data FROM chat_items WHERE session_id=? ORDER BY idx DESC LIMIT 1",
                (self.session_id,)).fetchone()
            if row is None:
                return None
            self._conn.execute("DELETE FROM chat_items WHERE session_id=? AND idx=?", (self.session_id, row[0]))
            self._conn.commit()
            return json.loads(row[1])

        return await asyncio.to_thread(impl)

    async def clear_session(self) -> None:
        def impl() -> None:
            self._conn.execute("DELETE FROM chat_items WHERE session_id=?", (self.session_id,))
            self._conn.commit()

        await asyncio.to_thread(impl)


def list_session_ids(db_path: Path) -> list[str]:
    import sqlite3

    if not db_path.exists():
        return []
    conn = sqlite3.connect(str(db_path))
    try:
        rows = conn.execute("SELECT session_id, MIN(rowid) FROM chat_items GROUP BY session_id ORDER BY MIN(rowid)").fetchall()
        return [str(r[0]) for r in rows]
    finally:
        conn.close()


def build_assistant(settings: Settings) -> Agent[AssistantContext]:
    set_default_openai_client(AsyncOpenAI(base_url=settings.agent_base_url, api_key=settings.agent_api_key))
    set_default_openai_api("chat_completions")
    set_tracing_disabled(True)
    return Agent(name="sdlc-assistant", instructions=_INSTRUCTIONS, tools=ASSISTANT_TOOLS,
                 model=settings.agent_model)


_SAFE_SID = re.compile(r"[^A-Za-z0-9._-]")
