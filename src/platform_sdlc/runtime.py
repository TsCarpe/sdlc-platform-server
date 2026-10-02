"""应用装配上下文：路由/服务共享的运行时对象（设置、路径、数据库、仓储、服务、agent）。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from platform_sdlc.config import ResolvedPaths, Settings
from platform_sdlc.db import Database
from platform_sdlc.repositories import Repos
from platform_sdlc.services import Services

if TYPE_CHECKING:
    from platform_sdlc.agent.loop import AgentLoop


@dataclass
class AppContext:
    settings: Settings
    paths: ResolvedPaths
    db: Database
    repos: Repos
    services: Services
    agent: AgentLoop | None = None  # P3 阶段无 agent；P4 lifespan 挂载
