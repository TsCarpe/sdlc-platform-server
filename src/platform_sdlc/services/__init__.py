"""服务层出口：统一组装点（依赖顺序：media/iteration/block → requirement/case/design → task → gate → orchestration）。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from platform_sdlc.db import Database
from platform_sdlc.repositories import Repos
from platform_sdlc.services.block import BlockService
from platform_sdlc.services.case import CaseService
from platform_sdlc.services.comment import CommentService
from platform_sdlc.services.design import DesignService
from platform_sdlc.services.gate import GateService
from platform_sdlc.services.iteration import IterationService
from platform_sdlc.services.media import MediaService
from platform_sdlc.services.orchestration import OrchestrationService
from platform_sdlc.services.requirement import RequirementService
from platform_sdlc.services.task import TaskService


@dataclass
class Services:
    media: MediaService
    iterations: IterationService
    blocks: BlockService
    comments: CommentService
    requirements: RequirementService
    cases: CaseService
    designs: DesignService
    tasks: TaskService
    gate: GateService
    orchestration: OrchestrationService

    @classmethod
    def create(cls, db: Database, repos: Repos, media_dir: Path, workspace_root: Path,
               media_max_bytes: int = 20 * 1024 * 1024) -> Services:
        media = MediaService(db, repos, media_dir, media_max_bytes)
        iterations = IterationService(db, repos)
        # v4.2/§3：workspace_root = 任务工作区统一根（source_import 完成回调按 tasks/<task_id>/out 定位，
        # 与进程内 executor、runner meta 下发三处同源此值——分叉则落库找不到文件）
        blocks = BlockService(db, repos, workspace_root)
        comments = CommentService(db, repos)
        requirements = RequirementService(db, repos, blocks)
        cases = CaseService(db, repos)
        designs = DesignService(db, repos)
        tasks = TaskService(db, repos, requirements, cases, designs, blocks)
        gate = GateService(db, repos, designs)
        orchestration = OrchestrationService(repos, tasks)
        return cls(media=media, iterations=iterations, blocks=blocks, comments=comments,
                   requirements=requirements, cases=cases,
                   designs=designs, tasks=tasks, gate=gate, orchestration=orchestration)
