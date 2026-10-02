"""迭代域服务（对应 IterationService，DESIGN-v2.4 D32/D33）。

1:1 语义 = 存活需求按 iter 唯一；绑定守卫主入口在 RequirementService.create（同事务），
本服务负责迭代自身的 CRUD。
"""

from __future__ import annotations

from typing import Any

from platform_sdlc.common import ApiException, Times, jrequired, jtext
from platform_sdlc.db import Database
from platform_sdlc.models import Iteration, IterationListRow
from platform_sdlc.repositories import Repos

NAME_MAX = 64
_NOTE_MAX = 200
_BY_MAX = 40


class IterationService:
    def __init__(self, db: Database, repos: Repos) -> None:
        self.db = db
        self.repos = repos

    def list(self) -> list[IterationListRow]:
        return self.repos.iteration.list_all()

    def get(self, name: str) -> IterationListRow:
        row = self.repos.iteration.find_row(name)
        if row is None:
            raise ApiException(404, f"迭代不存在：{name}")
        return row

    def create(self, body: dict[str, Any]) -> str:
        name = jrequired(body, "name").strip()
        if not name:
            raise ApiException(400, "迭代名不能为空")
        if len(name) > NAME_MAX:
            raise ApiException(400, f"迭代名过长（≤{NAME_MAX} 字，当前 {len(name)} 字）")
        note = jtext(body, "note", "").strip()
        if len(note) > _NOTE_MAX:
            raise ApiException(400, f"备注过长（≤{_NOTE_MAX} 字）")
        created_by = jtext(body, "createdBy", "").strip()
        if len(created_by) > _BY_MAX:
            raise ApiException(400, f"创建人过长（≤{_BY_MAX} 字）")
        with self.db.tx():
            if self.repos.iteration.exists(name):
                raise ApiException(409, f"迭代已存在：{name}")
            it = Iteration(name=name, note=note, created_by=created_by, created_at=Times.now())
            self.repos.iteration.insert(it)
        return it.name

    def delete(self, name: str) -> None:
        """仅空迭代可删（无存活绑定需求）；有绑定 → 409 指向绑定卡。迭代行无产物，物理删除。"""
        with self.db.tx():
            if self.repos.iteration.find(name) is None:
                raise ApiException(404, f"迭代不存在：{name}")
            bound = self.repos.requirement.alive_id_by_iter(name)
            if bound is not None:
                raise ApiException(409, f"迭代已绑定需求 {bound}（迭代与需求 1:1），先删除/移出需求再删迭代")
            self.repos.iteration.delete(name)
