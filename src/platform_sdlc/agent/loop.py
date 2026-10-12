"""进程内任务 agent 循环（对应 SdlcAgentService，asyncio 单循环串行，守卫不变）。

claim → AgentExecutor 执行 → 机械 selfCheck（草稿真回传且结构合法才记 done）→ done/failed。
verify 回归期间可通过 POST /api/agent/pause 暂停，避免抢领验证脚本自建自领的任务。
所有阻塞操作（sqlite/文件）经 asyncio.to_thread，事件循环不因任务执行冻结（P4 活性断言）。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from typing import Any

from platform_sdlc.agent.executor import AgentExecutor
from platform_sdlc.config import Settings
from platform_sdlc.models import Task
from platform_sdlc.repositories import Repos
from platform_sdlc.services import Services

log = logging.getLogger(__name__)


class AgentLoop:
    RUNNER_NAME = "openai-agents-inproc"

    def __init__(self, settings: Settings, repos: Repos, services: Services) -> None:
        self.settings = settings
        self.repos = repos
        self.services = services
        self.executor = AgentExecutor(settings, repos, services)
        self._paused = False
        self._alive = False
        self._runner: asyncio.Task[None] | None = None

    # ---------- 生命周期 ----------

    def start(self) -> None:
        self._runner = asyncio.get_running_loop().create_task(self._run())
        log.info("[agent] 进程内 agent 已启动：模型 %s @ %s，轮询 %dms，超时 %dmin",
                 self.settings.agent_model, self.settings.agent_base_url,
                 self.settings.agent_poll_interval_ms, self.settings.agent_timeout_min)

    async def stop(self) -> None:
        if self._runner is not None:
            self._runner.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):  # noqa: BLE001  # 停机路径尽力而为
                await self._runner
            self._runner = None

    # ---------- 状态与回归开关 ----------

    def status(self) -> dict[str, Any]:
        return {"enabled": True, "paused": self._paused, "running": self._alive}

    def pause(self) -> dict[str, Any]:
        self._paused = True
        return self.status()

    def resume(self) -> dict[str, Any]:
        self._paused = False
        return self.status()

    # ---------- 主循环 ----------

    async def _run(self) -> None:
        """主循环（自愈版，AUDIT D5-2）：异常退出指数退避重启（30s 起、5min 封顶）——一次意外
        不再等于通道永久停摆；CancelledError 是正常停机不重启；_paused 为实例属性，重启后语义保留。"""
        self._alive = True
        backoff = 30.0
        try:
            while True:
                try:
                    await self._loop_body()
                    return
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001  # 自愈重启：异常仅降级为一次退避等待
                    log.exception("[agent] 主循环异常退出，%.0fs 后重启（paused=%s）", backoff, self._paused)
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, 300.0)
        finally:
            self._alive = False

    async def _loop_body(self) -> None:
        # 归属过滤：只清本执行器名下任务（双通道恢复互不越界，AUDIT D1-3）
        recovered = await asyncio.to_thread(
            self.services.tasks.expire_stale, self.settings.agent_timeout_min, self.RUNNER_NAME)
        if recovered > 0:
            log.info("[agent] 启动恢复：%d 个超时 running 任务置 failed（runner=%s）", recovered, self.RUNNER_NAME)
        while True:
            if self._paused:
                await asyncio.sleep(1)
                continue
            t = await asyncio.to_thread(self.services.tasks.claim, self.RUNNER_NAME)
            if t is None:
                await asyncio.sleep(self.settings.agent_poll_interval_ms / 1000)
                continue
            await self._handle(t)

    async def _handle(self, t: Task) -> None:
        log.info("[agent] 领取 #%s %s @ %s", t.id, t.type, t.req_id)
        # 发布后兜底（评测 P0-2b）：claim 与发布之间需求可能已进终态，质检/合成类不再调模型，直接作废
        if await asyncio.to_thread(self.services.tasks.superseded_by_publish, t):
            await asyncio.to_thread(
                self.services.tasks.finish, t.id, False,
                {"error": "需求已发布（终态锁定），任务自动作废", "reason": "superseded-by-publish",
                 "hint": "标准文档已由人工定稿；如需重新质检请新开一轮需求"})
            log.info("[agent] #%s 随发布作废（终态锁），未调用模型", t.id)
            return

        res = None
        error: str | None = None
        reason = ""
        hint = ""
        diag = ""
        try:
            res = await self.executor.execute(t)
        except Exception as e:  # noqa: BLE001  # 执行异常进 failed 回执
            error = f"{type(e).__name__}: {e}"
            reason = "agent-exception"
            hint = "请重试；连续失败请检查 agent 配置与模型额度"
        if error is None:
            chk = await asyncio.to_thread(self.self_check, t)
            if chk is not None:  # 草稿未回传/结构不合法：结论给人，agent 输出尾段进 diag
                error = chk
                reason = "no-draft"
                hint = "请重试；连续失败请展开诊断，确认 agent 是否被守卫拒绝"
                diag = self._diag_text(res)
        try:
            if error is None:
                summary = res.summary if res is not None else None
                result = summary if summary else json.dumps({"draft": self._draft_type_of(t.type)}, ensure_ascii=False)
                await asyncio.to_thread(self.services.tasks.finish, t.id, True, {"result": result})
                log.info("[agent] #%s 完成：%s", t.id, result)
            else:
                await asyncio.to_thread(self.services.tasks.finish, t.id, False,
                                        {"error": error, "reason": reason, "hint": hint, "diag": diag})
                log.warning("[agent] #%s 失败（%s）：%s", t.id, reason, error)
        except Exception as e:  # noqa: BLE001  # 回报终态失败仅记日志
            log.error("[agent] #%s 回报终态失败：%s", t.id, e)

    # ---------- 机械自查（自 runner.mjs selfCheck / SdlcAgentService 逐条移植） ----------

    def self_check(self, t: Task) -> str | None:
        """草稿真的回传了、结构合法，才允许记 done；返回 None = 通过。"""
        draft_type = self._draft_type_of(t.type)
        try:
            d = self.services.tasks.get_draft(t.req_id, draft_type)
        except Exception:
            return f"草稿未回传（{draft_type}）"
        if draft_type == "standard":
            if not d.md or len(d.md.strip()) < 200:
                return "standard 草稿为空或过短"
            return None
        if draft_type == "design":
            # 机械齐套下限（DESIGN-v2.4 D34）：非空且含九节结构标头，防空壳草稿走完任务态
            if not d.md or len(d.md.strip()) < 200:
                return "design 草稿为空或过短"
            if "§0" not in d.md:
                return "design 草稿缺 §0–§8 结构标头（design-template 九节）"
            return None
        if draft_type == "gate_issues":
            # 机械齐套下限（DESIGN-v2.4 D35）：md 全文 + 结构化 issues 双载体齐全
            if not d.md or len(d.md.strip()) < 100:
                return "gate_issues 草稿缺 issues 文件全文（md）"
            try:
                p = json.loads(d.content_json or "{}")
                if not isinstance(p.get("issues"), list) or not p.get("issues"):
                    return "gate_issues 草稿 contentJson.issues 为空"
            except Exception as e:
                return f"gate_issues 草稿 contentJson 解析失败：{e}"
            return None
        try:
            p = json.loads(d.content_json or "{}")
            ok = (isinstance(p.get("digest"), dict) and isinstance(p.get("audit"), dict)
                  and isinstance(p.get("items"), list) and p["items"]) if draft_type == "digest_bundle" \
                else (isinstance(p.get("items"), list) and p["items"])
            if not ok:
                return "草稿 contentJson 结构不完整（缺 digest/audit/items）"
            return None
        except Exception as e:
            return f"草稿 contentJson 解析失败：{e}"

    @staticmethod
    def _draft_type_of(task_type: str) -> str:
        if task_type == "quality_check":
            return "digest_bundle"
        if task_type == "standard_doc":
            return "standard"
        if task_type in ("design_gen", "design_rev"):
            return "design"
        if task_type == "gate_review":
            return "gate_issues"
        return "cases"

    @staticmethod
    def _diag_text(res: Any) -> str:
        """agent 最终输出尾部（「未完成原因」通常在结尾），进失败 diag，上限 800 字符。"""
        text = (getattr(res, "final_text", None) or "").strip()
        if not text:
            return "（agent 无最终输出）"
        return text if len(text) <= 800 else "…" + text[-800:]
