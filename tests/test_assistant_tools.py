"""P0-1 验收：助理只读工具 repos 装配——get_requirement_summary 数据接地（修复前 s.repos 必 AttributeError）。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from platform_sdlc.agent.assistant import AssistantContext, _requirement_summary  # type: ignore[attr-defined]
from platform_sdlc.db import Database
from platform_sdlc.migrations import run_migrations
from platform_sdlc.models import Requirement
from platform_sdlc.repositories import Repos
from platform_sdlc.services import Services

NOW = "2026-10-01 10:00:00"


@pytest.fixture()
def env(tmp_path: Path) -> tuple[Repos, Services]:
    db = Database(tmp_path / "platform.db")
    run_migrations(db)
    repos = Repos.create(db)
    services = Services.create(db, repos, tmp_path / "media")
    return repos, services


def make_req(repos: Repos) -> str:
    r = Requirement(id="", name="测试需求", iter="", source_type="text", source_text="# 原文",
                    status="clarifying", submitted_by="t", standard_doc="",
                    created_at=NOW, synced_at=NOW, risk_tier="B")
    r.id = repos.requirement.next_id("2026")
    repos.requirement.insert(r)
    return r.id


def test_requirement_summary_grounded(env: tuple[Repos, Services]) -> None:
    """摘要的计数必须来自真实数据（repos 装配修复后的主路径）。"""
    repos, services = env
    req_id = make_req(repos)
    services.requirements.push_artifacts(req_id, {
        "digest": {"md": "# d", "risk_tier": "B", "fr_points": [{"id": "需求.FR-01", "name": "登录"}]},
        "audit": {"md": "# a", "red": 0, "yellow": 0, "blue": 0, "items": []},
        "items": [{"seq": "P01", "level": "yellow", "theme": "登录", "question": "密码规则？"}],
    })
    services.requirements.set_answer(req_id, "P01", {"answer": "至少 8 位"})
    services.cases.sync_upsert(None, {"reqId": req_id, "items": [{"id": "TC-1", "title": "登录用例"}]})

    out = json.loads(_requirement_summary(services, repos, req_id))
    assert out["frCount"] == 1
    assert out["caseCount"] == 1
    assert out["clarify"] == "1/1"
    assert out["designPhase"] == "none" and out["gatePhase"] == "blocked"  # 未定稿/未开闸的初始态


def test_assistant_context_shape(env: tuple[Repos, Services]) -> None:
    """契约守卫：AssistantContext 必须同时携带 services 与 repos（防装配回退）。"""
    repos, services = env
    c = AssistantContext(services=services, repos=repos)
    assert c.services is services and c.repos is repos
