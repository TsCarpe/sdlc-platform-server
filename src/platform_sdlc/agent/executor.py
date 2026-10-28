"""openai-agents 任务执行器（对应 AgentScopeAgentExecutor，Phase ②）。

- prompt 权威在 agent/prompts.py（runner.mjs 下线后单通道唯一源）；
- 兼容端点接入：set_default_openai_client + chat_completions + tracing off（DeepSeek/GLM）；
- 结果解析保留 SDLC_TASK_RESULT 正则（verify 契约；output_type 在 DeepSeek chat_completions 上
  的支持度未经验证，不强制结构化输出——评审 P1-3 决策）；
- DeepSeek DSML 泄漏兜底改写：AgentScope 特征串不可移植，改为「最终输出缺结果行 → 换会话续跑一次」
  （磁盘中介下工作区产物仍在，续跑不重做，与 Java 每次尝试独立 sessionId 的语义一致）。
"""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass
from pathlib import Path

from agents import (
    Agent,
    RunContextWrapper,
    Runner,
    Tool,
    function_tool,
    set_default_openai_api,
    set_default_openai_client,
    set_tracing_disabled,
)
from openai import AsyncOpenAI

from platform_sdlc.agent import prompts
from platform_sdlc.agent.context import TaskRunContext
from platform_sdlc.agent.tools import build_tools, bump_fanout_count, get_design_doc, get_digest_bundle, list_cases, read_file
from platform_sdlc.config import Settings
from platform_sdlc.integrations.feishu import FeishuClient
from platform_sdlc.models import Task
from platform_sdlc.repositories import Repos
from platform_sdlc.services import Services

_RESULT_LINE = re.compile(r"SDLC_TASK_RESULT\s+(\{.*\})\s*$", re.MULTILINE)

# gate_review 扇出子代理的只读工具面（无写入：子代理只评审回报，草稿由主会话统一回传）
_REVIEWER_TOOLS: list[Tool] = [get_digest_bundle, get_design_doc, list_cases, read_file]
_SUB_MAX_TURNS = 30  # SDK 默认 10 不够（三件输入 + 技能模板）；主 run 上限仍是 agent_max_iters
_SUB_TIMEOUT_S = 15 * 60  # 单个角色子 run 预算（外层 gate_timeout_min 管总墙钟）


def _err(msg: str) -> str:
    return json.dumps({"error": msg}, ensure_ascii=False)


def build_spawn_reviewer(model: str, role_prompts: dict[str, str]) -> Tool:
    """gate_review 专用扇出工具（P1-9 语义进程内承载，仅 A/B 级任务装配）。

    每次调用 = 一个全新上下文子代理（独立 Agent 实例 + 独立 Runner.run，机械满足
    contextMode=fresh）；实际成功次数持久化到工作区 gate-fanout.json（bump_fanout_count），
    put_draft 按它核对留痕 fanout——禁止单会话冒充扇出（少扇出写 external/N 会被拒收）。"""

    @function_tool
    async def spawn_reviewer(wrapper: RunContextWrapper[TaskRunContext], role: str, brief: str) -> str:
        """扇出一个全新上下文的独立评审子代理（P1-9 扇出机械件）。每个角色各调用一次；
        交叉审查角色只产分歧。返回子代理评审报告；扇出计数由平台持久化，留痕 fanout 须与之一致。

        Args:
            role: 角色标识（本任务可用角色见参数错误提示；architecture/data-model/testability/cross-check）
            brief: 该角色的评审重点提示（一两句即可；子代理自行读取输入三件，不需贴正文）
        """
        ctx = wrapper.context
        if role not in role_prompts:
            return _err(f"角色非法：{role}（本任务可用：{'/'.join(role_prompts)}）")
        sub = Agent(name=f"sdlc-gate-{role}", instructions=role_prompts[role],
                    tools=_REVIEWER_TOOLS, model=model)
        try:
            run = await asyncio.wait_for(
                Runner.run(sub, brief or role, context=ctx, max_turns=_SUB_MAX_TURNS), _SUB_TIMEOUT_S)
        except TimeoutError:
            return _err(f"子代理超时（{_SUB_TIMEOUT_S // 60}min）：{role}——请精简 brief 后重试该角色")
        except Exception as e:  # noqa: BLE001  # 子 run 异常回灌主会话，可重试该角色
            return _err(f"子代理执行失败（{role}）：{type(e).__name__}: {e}——请重试该角色一次")
        text = str(run.final_output or "")
        calls = await asyncio.to_thread(bump_fanout_count, ctx.workspace, ctx.task_id)
        lim = 24000
        report = text if len(text) <= lim else text[:lim] + f"...[截断，共 {len(text)} 字符]"
        return json.dumps({"ok": True, "role": role, "fanoutCalls": calls, "report": report},
                          ensure_ascii=False)

    return spawn_reviewer


@dataclass
class AgentExecutionResult:
    summary: str | None
    final_text: str


class AgentExecutor:
    def __init__(self, settings: Settings, repos: Repos, services: Services) -> None:
        self.settings = settings
        self.repos = repos
        self.services = services
        self._configured = False

    def configure(self) -> None:
        """全局模型接线（一次）：OpenAI 兼容端点走 Chat Completions；无 OpenAI key 关 traces。"""
        if self._configured:
            return
        if not (self.settings.agent_base_url and self.settings.agent_api_key and self.settings.agent_model):
            raise RuntimeError("agent 未配置：需 SDLC_AGENT_BASE_URL / SDLC_AGENT_API_KEY / SDLC_AGENT_MODEL")
        set_default_openai_client(AsyncOpenAI(base_url=self.settings.agent_base_url,
                                              api_key=self.settings.agent_api_key))
        set_default_openai_api("chat_completions")
        set_tracing_disabled(True)
        self._configured = True

    async def execute(self, task: Task) -> AgentExecutionResult:
        self.configure()
        r = self.repos.requirement.find(task.req_id)
        if r is None:
            raise RuntimeError(f"需求不存在：{task.req_id}")
        skills_repo = Path(self.settings.agent_skills_repo).expanduser()
        if not skills_repo.is_dir():
            raise RuntimeError(f"技能仓不存在：{skills_repo}（请配置 SDLC_AGENT_SKILLS_REPO）")
        safe_name = re.sub(r"[/\\:*?\"<>|]", "_", r.name or task.req_id)
        workspace = (Path(self.settings.agent_workspace_root) / "sdlc" / safe_name).resolve()
        workspace.mkdir(parents=True, exist_ok=True)

        # 飞书能力（§6）：凭据齐备才构造客户端——工具注册 / prompt 分支 / 命令白名单三处
        # 以 ctx.feishu 是否为 None 联动（None = 无能力，不假绿）
        feishu = (FeishuClient(self.settings.feishu_app_id, self.settings.feishu_app_secret)
                  if self.settings.feishu_app_id and self.settings.feishu_app_secret else None)
        tools = build_tools(feishu)

        repos_of = self.services.designs.repos_of(task.req_id) if task.type == "design_gen" else []
        # v4-3（DESIGN-v4 §3）：source_import 输出目录按任务 ID 定位（<根>/tasks/<task_id>/out），
        # 根与服务端完成回调同源（services.blocks.workspace_root = ResolvedPaths.workspace_root）——
        # 分叉则落库找不到文件；agent 作业区（sdlc/<safe_name>）语义不变，仅导入产物走绝对路径。
        import_out_dir = (self.services.blocks.workspace_root / "tasks" / str(task.id) / "out").resolve()
        # P1-9 扇出（runner 2026-10-28 下线后进程内承载）：A/B 级 gate_review 装配扇出工具
        # （B 级 = data-model + cross-check，A 级四角色）；C 级主会话自查不装配
        tier = (r.risk_tier or "A") if task.type == "gate_review" else "C"
        if task.type == "gate_review" and tier in ("A", "B"):
            role_prompts = {k: prompts.gate_role_prompt(k, str(skills_repo))
                            for k in prompts.gate_fanout_roles(tier)}
            tools = tools + [build_spawn_reviewer(self.settings.agent_model, role_prompts)]
        instructions = prompts.sys_prompt(
            task.type, r, skills_repo, repos_of,
            import_out_dir=import_out_dir if task.type == "source_import" else None,
            feishu=feishu is not None)

        # gate 专项墙钟：主会话 + 4 子 run + 汇合共享同一 deadline，默认 30min 对 A 级太紧
        timeout_min = (self.settings.gate_timeout_min if task.type == "gate_review"
                       else self.settings.agent_timeout_min)
        deadline = asyncio.get_running_loop().time() + timeout_min * 60
        text = ""
        # 续跑兜底（DSML 替代检测）：attempt=2 或人工 retry 时工作区已有产物，续跑提示告知盘点不重做
        for attempt in (1, 2):
            agent = Agent(
                name=f"sdlc-{task.type}",
                instructions=instructions,
                tools=tools,
                model=self.settings.agent_model,
            )
            ctx = TaskRunContext(req_id=task.req_id, task_id=task.id, task_type=task.type,
                                 workspace=workspace, skills_repo=skills_repo, services=self.services,
                                 repos=self.repos, feishu=feishu,
                                 feishu_media_max_bytes=self.settings.feishu_media_max_bytes,
                                 tier=tier)
            goal = prompts.goal_prompt(task.type) + prompts.resume_hint(workspace)
            if task.type == "design_rev":
                goal += "\n本次落改工作项（任务备注）：\n" + prompts.payload_note(task.payload)
            if attempt == 2:
                goal += "\n（注：上一次会话最后一步的工具调用格式损坏未被执行；请基于现状继续完成剩余步骤，不要重做已完成的步骤。）"
            remain = deadline - asyncio.get_running_loop().time()
            if remain <= 0:
                raise TimeoutError(f"任务超时（{timeout_min}min）")
            run = await asyncio.wait_for(
                Runner.run(agent, goal, context=ctx, max_turns=self.settings.agent_max_iters), remain)
            text = str(run.final_output or "")
            if _RESULT_LINE.search(text.strip()):
                break  # 结果行在 = 正常收尾；缺失才续跑（替代 Java 的 DSML 标记检测）
        m = _RESULT_LINE.search(text.strip())
        return AgentExecutionResult(summary=m.group(1) if m else None, final_text=text)
