"""服务层出口：统一组装点（依赖顺序：media/iteration → requirement/case/design → task → gate → orchestration）。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from platform_sdlc.db import Database
from platform_sdlc.repositories import Repos
from platform_sdlc.services.case import CaseService
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
    requirements: RequirementService
    cases: CaseService
    designs: DesignService
    tasks: TaskService
    gate: GateService
    orchestration: OrchestrationService

    @classmethod
    def create(cls, db: Database, repos: Repos, media_dir: Path,
               media_max_bytes: int = 20 * 1024 * 1024) -> Services:
        media = MediaService(db, repos, media_dir, media_max_bytes)
        iterations = IterationService(db, repos)
        requirements = RequirementService(db, repos)
        cases = CaseService(db, repos)
        designs = DesignService(db, repos)
        tasks = TaskService(db, repos, requirements, cases, designs)
        gate = GateService(db, repos, designs, tasks)
        orchestration = OrchestrationService(repos, tasks)
        return cls(media=media, iterations=iterations, requirements=requirements, cases=cases,
                   designs=designs, tasks=tasks, gate=gate, orchestration=orchestration)
