"""P0-1 验收：助理只读工具 repos 装配——get_requirement_summary 数据接地（修复前 s.repos 必 AttributeError）。"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import pytest

from platform_sdlc.agent.assistant import AssistantContext, _requirement_summary  # type: ignore[attr-defined]
from platform_sdlc.agent.tools import _split_segments
from platform_sdlc.db import Database
from platform_sdlc.migrations import run_migrations
from platform_sdlc.models import Requirement
from platform_sdlc.repositories import Repos
from platform_sdlc.services import Services

NOW = "2026-10-01 10:00:00"


@pytest.fixture()
def env(tmp_path: Path, make_database: Callable[..., Database]) -> tuple[Repos, Services]:
    db = make_database()
    run_migrations(db)
    repos = Repos.create(db)
    services = Services.create(db, repos, tmp_path / "media", tmp_path)
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


# ---------- run_command 引号感知分段（review #23）：shlex POSIX 令牌流与 sh -c 词法一致 ----------

def test_split_segments_quote_aware() -> None:
    """#23 目标场景：引号内 ; | 是字面量不切段——带分号 URL 的 curl 直落 raw/ 主路径放行；
    单行 python3 -c 整体加引号即可执行（原 regex 误切段拒执行的可用性限制解除）。"""
    assert _split_segments('curl -s "http://x/a;b" > raw/d.json') == ["curl"]
    assert _split_segments('node -e "console.log(1);console.log(2)"') == ["node"]
    assert _split_segments("python3 -c 'import json; d=json.load(open(\"x\"))'") == ["python3"]


def test_split_segments_operators_outside_quotes() -> None:
    """#23 安全底线：引号外 ;/|/&& 仍切段——每段段首命令独立暴露给白名单校验
    （`curl -s x; sh evil.sh` 的第二段段首 sh 不在白名单必拒）。"""
    assert _split_segments("curl -s x; sh evil.sh") == ["curl", "sh"]
    assert _split_segments("mkdir -p raw && curl -s y | jq .") == ["mkdir", "curl", "jq"]


def test_split_segments_backslash_quote_bypass_locked() -> None:
    """#23 绕过锁定（评估 A1 反面用例）：`\\"` 是 sh 转义字面引号、其后的 `;` 是真分隔符——
    朴素手写引号状态机会误判「引号内」不切段，令后续命令逃逸白名单；shlex POSIX 词法正确
    切成两段（段首 sh 暴露给白名单校验必拒）。fail-closed 前置：修此条前旧版按 `;` 无条件
    切段同样拒绝，改错（状态机）才会开洞——本用例锁定改对。"""
    assert _split_segments('curl \\"; sh evil.sh') == ["curl", "sh"]


def test_split_segments_malformed_rejected() -> None:
    """#23 fail-closed：未闭合引号 shlex 抛 ValueError、操作符悬空/连打/子 shell 括号成空段
    显式抛 ValueError——一律拒绝，不猜词法补全。"""
    for bad in ("curl 'abc", "curl -s x ;", "a ; | b", "( sh evil.sh )"):
        with pytest.raises(ValueError):
            _split_segments(bad)
