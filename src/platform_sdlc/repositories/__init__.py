"""仓储层出口：统一组装点（对应 Java Spring @Repository 装配）。"""

from __future__ import annotations

from dataclasses import dataclass

from platform_sdlc.db import Database
from platform_sdlc.repositories.artifact_repo import ArtifactRepo
from platform_sdlc.repositories.clarify_item_repo import ClarifyItemRepo
from platform_sdlc.repositories.comment_repo import CommentRepo
from platform_sdlc.repositories.draft_repo import DraftRepo
from platform_sdlc.repositories.fr_point_repo import FrPointRepo
from platform_sdlc.repositories.gate_repo import GateRepo
from platform_sdlc.repositories.iteration_repo import IterationRepo
from platform_sdlc.repositories.media_repo import MediaRepo
from platform_sdlc.repositories.requirement_repo import RequirementRepo
from platform_sdlc.repositories.task_repo import TaskRepo
from platform_sdlc.repositories.test_case_repo import TestCaseRepo


@dataclass
class Repos:
    """全部仓储实例（单库单套，启动时组装一次）。"""

    requirement: RequirementRepo
    clarify_item: ClarifyItemRepo
    artifact: ArtifactRepo
    fr_point: FrPointRepo
    media: MediaRepo
    test_case: TestCaseRepo
    comment: CommentRepo
    task: TaskRepo
    draft: DraftRepo
    iteration: IterationRepo
    gate: GateRepo

    @classmethod
    def create(cls, db: Database) -> Repos:
        return cls(
            requirement=RequirementRepo(db),
            clarify_item=ClarifyItemRepo(db),
            artifact=ArtifactRepo(db),
            fr_point=FrPointRepo(db),
            media=MediaRepo(db),
            test_case=TestCaseRepo(db),
            comment=CommentRepo(db),
            task=TaskRepo(db),
            draft=DraftRepo(db),
            iteration=IterationRepo(db),
            gate=GateRepo(db),
        )
