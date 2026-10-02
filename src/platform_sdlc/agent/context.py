"""单次任务运行上下文（对应 Java AgentPlatformTools 的构造注入面）。

红线表达：本上下文只装配只读服务 + put_draft/upload_media 两个写入面——
正式产物写入通道（publish/approve/artifacts/cases 表单面）在上下文中【不存在】。
context 是纯本地对象，不会进入 LLM 消息。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from platform_sdlc.services import Services


@dataclass
class TaskRunContext:
    req_id: str
    task_id: int
    task_type: str
    workspace: Path
    skills_repo: Path
    services: Services
