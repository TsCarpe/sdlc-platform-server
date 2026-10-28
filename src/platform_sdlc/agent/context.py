"""单次任务运行上下文（对应 Java AgentPlatformTools 的构造注入面）。

红线表达：本上下文只装配只读服务 + put_draft/upload_media 两个写入面——
正式产物写入通道（publish/approve/artifacts/cases 表单面）在上下文中【不存在】。
context 是纯本地对象，不会进入 LLM 消息。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from platform_sdlc.integrations.feishu import FeishuClient
from platform_sdlc.repositories import Repos
from platform_sdlc.services import Services


@dataclass
class TaskRunContext:
    req_id: str
    task_id: int
    task_type: str
    workspace: Path
    skills_repo: Path
    services: Services
    repos: Repos
    # 飞书客户端（DESIGN-飞书接入Python化 §6）：凭据不进 os.environ、不进 LLM 上下文——
    # executor 按 Settings 构造注入；None = 无飞书能力（feishu_fetch 不注册、prompt 无飞书分支）
    feishu: FeishuClient | None = None
    feishu_media_max_bytes: int = 10 * 1024 * 1024  # 大附件阈值（Settings 注入）
