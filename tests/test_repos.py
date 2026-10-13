"""P1 验收：仓储层读写链路（新库全链路 + 存量库副本抽查）。"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from platform_sdlc.db import Database
from platform_sdlc.migrations import run_migrations
from platform_sdlc.models import (
    CaseComment,
    ClarifyItem,
    DraftArtifact,
    FrPoint,
    GateIssue,
    GateRound,
    Iteration,
    MediaFile,
    Requirement,
    Task,
)
from platform_sdlc.models import TestCase as TestCaseEntity  # 别名：避免 pytest 收集 Test* 类
from platform_sdlc.repositories import Repos

# 存量库副本抽查（可选）：SDLC_EXISTING_DB=/path/to/platform.db uv run pytest 才启用；
# 默认跳过（原 backend/ 为 Java v2 时代目录已删除，原硬编码路径恒 skipif——AUDIT D7-5）
EXISTING_DB = Path(os.environ["SDLC_EXISTING_DB"]).resolve() if os.environ.get("SDLC_EXISTING_DB") else None


@pytest.fixture()
def repos(tmp_path: Path) -> Repos:
    db = Database(tmp_path / "platform.db")
    run_migrations(db)
    return Repos.create(db)


def test_requirement_lifecycle(repos: Repos) -> None:
    r = Requirement(
        id="REQ-2026-001",
        name="区域教学技能比赛",
        iter="2026-W40",
        source_type="text",
        source_text="# 原文",
        status="submitted",
        submitted_by="tester",
        created_at="2026-10-01 10:00:00",
        synced_at="2026-10-01 10:00:00",
    )
    repos.requirement.insert(r)
    assert repos.requirement.next_id("2026") == "REQ-2026-002"
    found = repos.requirement.find("REQ-2026-001")
    assert found is not None and found.name == "区域教学技能比赛" and found.risk_tier == ""

    # 软删后 find 404、软删编号参与取号（001 不复用但计入 max）、迭代名释放
    assert repos.requirement.soft_delete("REQ-2026-001", "2026-10-02 10:00:00") == 1
    assert repos.requirement.find("REQ-2026-001") is None
    assert repos.requirement.next_id("2026") == "REQ-2026-002"  # 已删的 001 计入 max，不回发
    assert repos.requirement.alive_id_by_iter("2026-W40") is None


def test_clarify_and_counts(repos: Repos) -> None:
    repos.requirement.insert(Requirement(id="REQ-1", name="n", source_type="text", status="clarifying", created_at="2026-10-01 10:00:00"))
    for seq, answer, status in (("P01", "", "pending"), ("P02", "已答复", "answered"), ("P03", "", "rejected")):
        repos.clarify_item.insert(ClarifyItem(id=0, req_id="REQ-1", seq=seq, level="red", question=f"Q{seq}", answer=answer, status=status))
    counts = repos.clarify_item.counts("REQ-1")
    assert counts == {"total": 3, "answered": 2}
    disp = repos.clarify_item.disposition_map_by_seq("REQ-1")
    assert set(disp) == {"P02", "P03"}  # pending 纯净行不携带
    assert repos.clarify_item.set_disposition("REQ-1", "P01", "答复了", "answered", "", "2026-10-01 11:00:00") == 1


def test_artifact_version_increment(repos: Repos) -> None:
    repos.artifact.upsert("REQ-1", "digest", "# md", "{}", "2026-10-01 10:00:00")
    repos.artifact.upsert("REQ-1", "digest", "# md2", "{}", "2026-10-01 11:00:00")
    a = repos.artifact.find("REQ-1", "digest")
    assert a is not None and a.version == 2 and a.markdown == "# md2"


def test_task_claim_finish_atomic(repos: Repos) -> None:
    repos.requirement.insert(Requirement(id="REQ-1", name="n", source_type="text", status="clarifying", created_at="2026-10-01 10:00:00"))
    t = Task(id=0, req_id="REQ-1", type="quality_check", created_by="tester", created_at="2026-10-01 10:00:00")
    t.id = repos.task.insert(t)
    assert repos.task.count_active("REQ-1", "quality_check") == 1

    claimed_a = repos.task.claim("runner-a", "2026-10-01 10:01:00")
    assert claimed_a is not None and claimed_a.status == "running"
    assert repos.task.claim("runner-b", "2026-10-01 10:01:01") is None  # 队列空

    assert repos.task.finish(t.id, "done", '{"ok":1}', "2026-10-01 10:05:00")
    assert not repos.task.finish(t.id, "failed", "{}", "2026-10-01 10:05:01")  # 仅 running 可回报


def test_case_no_monotonic_ledger(repos: Repos) -> None:
    repos.requirement.insert(Requirement(id="REQ-1", name="n", source_type="text", status="clarifying", created_at="2026-10-01 10:00:00"))
    t1 = TestCaseEntity(req_id="REQ-1", id="CASE-1", title="t1", created_at="2026-10-01 10:00:00", updated_at="2026-10-01 10:00:00")
    repos.test_case.insert(t1)
    assert t1.case_no == 1
    start = repos.test_case.allocate_case_nos(3)
    assert start == 2  # 账本 val=4，起始 2..4
    repos.test_case.delete("REQ-1", "CASE-1")
    t2 = TestCaseEntity(req_id="REQ-1", id="CASE-2", title="t2", created_at="2026-10-01 10:01:00", updated_at="2026-10-01 10:01:00")
    repos.test_case.insert(t2)
    assert t2.case_no == 5  # 硬删不复用号


def test_draft_upsert_and_gate(repos: Repos) -> None:
    repos.requirement.insert(Requirement(id="REQ-1", name="n", source_type="text", status="clarifying", created_at="2026-10-01 10:00:00"))
    repos.draft.upsert(
        DraftArtifact(req_id="REQ-1", type="cases", content_json='{"items":[1]}', task_id=7, updated_at="2026-10-01 10:00:00"),
        "2026-10-01 10:00:00",
    )
    d = repos.draft.find("REQ-1", "cases")
    assert d is not None and d.task_id == 7
    assert repos.draft.delete("REQ-1", "cases") == 1

    round_id = repos.gate.insert_round(
        GateRound(id=None, req_id="REQ-1", round_no=1, status="pending", created_by="tester", opened_at="2026-10-01 10:00:00")
    )
    repos.gate.insert_issues([GateIssue(id=None, round_id=round_id, code="A-01", merged_unit=3)])
    issue = repos.gate.list_issues(round_id)[0]
    assert repos.gate.apply_verdict(round_id, 3, "reconcile", "备注", "pm", "2026-10-01 11:00:00") == 1
    assert repos.gate.find_issue(issue.id).verdict == "reconcile"  # type: ignore[union-attr]
    assert repos.gate.set_released(round_id, "pm", "2026-10-01 12:00:00") == 1
    assert repos.gate.list_rounds("REQ-1")[0].status == "released"


def test_media_iteration_comment_frpoint(repos: Repos) -> None:
    repos.requirement.insert(Requirement(id="REQ-1", name="n", source_type="text", status="clarifying", created_at="2026-10-01 10:00:00"))
    repos.media.insert(
        MediaFile(req_id="REQ-1", media_id="m-abcdef123456", name="a.png", mime="image/png", bytes=3, created_at="2026-10-01 10:00:00")
    )
    assert repos.media.exists("REQ-1", "m-abcdef123456")

    repos.iteration.insert(Iteration(name="2026-W40", created_at="2026-10-01 10:00:00"))
    assert repos.iteration.exists("2026-W40")
    repos.iteration.ensure("2026-W40", "2026-10-02 10:00:00")  # 幂等

    repos.fr_point.replace_all("REQ-1", [FrPoint(req_id="REQ-1", id="需求.FR-01", name="登录")])
    assert repos.fr_point.exists("REQ-1", "需求.FR-01")

    repos.test_case.insert(
        TestCaseEntity(req_id="REQ-1", id="WEB-001", title="t", created_at="2026-10-01 10:00:00", updated_at="2026-10-01 10:00:00")
    )
    repos.comment.insert(CaseComment(id=0, req_id="REQ-1", case_id="WEB-001", by="pm", text="看下", created_at="2026-10-01 10:00:00"))
    assert repos.comment.list_by_case("REQ-1", "WEB-001")[0].text == "看下"
    assert repos.test_case.next_web_id("REQ-1") == "WEB-002"


@pytest.mark.skipif(not (EXISTING_DB and EXISTING_DB.exists()), reason="无存量库（SDLC_EXISTING_DB 未配置）")
def test_existing_db_smoke(tmp_path: Path) -> None:
    """存量库副本：迁移幂等 + 列表聚合可读（抽查真实数据形状）。"""
    work = tmp_path / "existing"
    work.mkdir()
    shutil.copy(EXISTING_DB, work / "platform.db")
    db = Database(work / "platform.db")
    run_migrations(db)
    run_migrations(db)  # 幂等
    repos = Repos.create(db)
    rows = repos.requirement.list_all(None, None)
    assert isinstance(rows, list)
    cases = repos.test_case.list(None, None, None, None, None)
    for c in cases[:5]:
        assert isinstance(c.verify_refs, list) and isinstance(c.case_no, int | type(None))
    iters = repos.iteration.list_all()
    for it in iters[:5]:
        assert it.design_phase in ("none", "draft", "final", "skipped")
        assert it.gate_stage in ("none", "active", "released")
