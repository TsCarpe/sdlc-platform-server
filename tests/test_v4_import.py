"""v4-3 验收：source_import 任务通道（DESIGN-v4 §3）——封存守卫、finish 完成回调落库、
noDraftTypes 两处代码分支、task_rules 出参、三处一致性守护纯函数。"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from typing import Any

import pytest

from platform_sdlc.agent.loop import AgentLoop
from platform_sdlc.config import Settings
from platform_sdlc.db import Database
from platform_sdlc.migrations import run_migrations
from platform_sdlc.models import Requirement
from platform_sdlc.repositories import Repos
from platform_sdlc.services import Services

_spec = importlib.util.spec_from_file_location(
    "check_task_templates", Path(__file__).resolve().parent.parent / "scripts" / "check_task_templates.py")
assert _spec is not None and _spec.loader is not None
_ctt = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_ctt)

_DOC = {
    "blocks": [
        {"id": "wk-h1", "type": "heading", "level": 1, "text": "背景", "path": "背景"},
        {"id": "wk-p1", "type": "paragraph", "text": "第一段", "path": "背景", "comments": "c1"},
        {"id": "cm-1", "type": "comment", "ref": "c1", "anchorBlockId": "wk-p1", "quote": "第一段",
         "thread": [{"user": "张三", "text": "问一下"}, {"user": "李四", "text": "答"}],
         "reactions": None, "path": "评论区"},
        {"id": "wk-img", "type": "image", "media": "m-ghost", "text": "", "path": "背景"},
    ],
    "media": [],
}


@pytest.fixture()
def env(tmp_path: Path) -> tuple[Database, Repos, Services, Path]:
    db = Database(tmp_path / "platform.db")
    run_migrations(db)
    repos = Repos.create(db)
    services = Services.create(db, repos, tmp_path / "media", tmp_path)
    return db, repos, services, tmp_path


def _mk_card(repos: Repos, req_id: str = "REQ-2026-001", source_type: str = "text") -> None:
    repos.requirement.insert(Requirement(
        id=req_id, name="导入验证", source_type=source_type,
        source_url="https://feishu.example/x" if source_type == "feishu" else "",
        source_text="# 原文" if source_type == "text" else "",
        status="submitted", submitted_by="t",
    ))


def _mk_out(ws: Path, task_id: int, doc: dict[str, Any]) -> Path:
    out = ws / "tasks" / str(task_id) / "out"
    out.mkdir(parents=True, exist_ok=True)
    p = out / "doc.json"
    p.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    return p


# ---------- 类型注册与封存守卫 ----------

def test_source_import_type_registered(env: tuple[Database, Repos, Services, Path]) -> None:
    db, repos, services, _ = env
    from platform_sdlc.common import ApiException

    _mk_card(repos)
    t = services.tasks.create_guarded("REQ-2026-001", "source_import", "", "")
    assert t.status == "queued" and t.type == "source_import"
    # published 终态锁照拒（source_import 不进发布后豁免面——published 卡无原文变更通道，走变更单）
    db.execute("UPDATE requirement SET status='published' WHERE id='REQ-2026-001'")
    with pytest.raises(ApiException, match="终态"):
        services.tasks.create_guarded("REQ-2026-001", "source_import", "", "")


def test_import_seal_guards(env: tuple[Database, Repos, Services, Path]) -> None:
    _, repos, services, _ = env
    from platform_sdlc.common import ApiException
    from platform_sdlc.models import ReqBlock, ReqComment

    # text 源：块表空 = 首导合法
    _mk_card(repos, "REQ-2026-001", "text")
    services.tasks.create_guarded("REQ-2026-001", "source_import", "", "")
    # 块表非空 = 封存
    repos.req_block.replace_all("REQ-2026-001", [
        ReqBlock(req_id="REQ-2026-001", block_id="x-1", seq=1000, type="paragraph", text="a")])
    with pytest.raises(ApiException, match="封存"):
        services.tasks.create_guarded("REQ-2026-001", "source_import", "", "")

    # feishu 源：无痕迹合法；编辑痕迹 / 平台评论任一即封存
    _mk_card(repos, "REQ-2026-002", "feishu")
    services.tasks.create_guarded("REQ-2026-002", "source_import", "", "")
    repos.req_block.replace_all("REQ-2026-002", [
        ReqBlock(req_id="REQ-2026-002", block_id="blk-new", seq=1000, type="paragraph", text="平台编辑")])
    with pytest.raises(ApiException, match="封存"):
        services.tasks.create_guarded("REQ-2026-002", "source_import", "", "")
    repos.req_block.replace_all("REQ-2026-002", [
        ReqBlock(req_id="REQ-2026-002", block_id="fs-1", seq=1000, type="paragraph", text="a")])
    repos.req_comment.insert(ReqComment(
        id=0, req_id="REQ-2026-002", block_id="fs-1", origin="platform", author="王五", text="平台评论"))
    with pytest.raises(ApiException, match="封存"):
        services.tasks.create_guarded("REQ-2026-002", "source_import", "", "")


# ---------- finish 完成回调落库 ----------

def test_finish_imports_blocks_and_comments(env: tuple[Database, Repos, Services, Path]) -> None:
    _, repos, services, ws = env
    _mk_card(repos)
    t = services.tasks.create_guarded("REQ-2026-001", "source_import", "", "")
    claimed = services.tasks.claim("test-runner")
    assert claimed is not None and claimed.id == t.id
    _mk_out(ws, t.id, _DOC)

    out = services.tasks.finish(t.id, True, {"result": "agent 摘要"})
    assert out.status == "done"
    # result 被落库统计覆盖（agent 摘要不进正式面）
    stats = json.loads(out.result)
    assert stats == {"blocks": 3, "comments": 2, "missingMedia": 1}

    blocks = repos.req_block.list_by_req("REQ-2026-001")
    assert [b.block_id for b in blocks] == ["wk-h1", "wk-p1", "wk-img"]
    assert [b.seq for b in blocks] == [1000, 2000, 4000]  # comment 块占位 seq 3000（与迁移同语义）
    assert json.loads(blocks[0].attrs_json) == {"level": 1}  # path 派生不落
    cms = repos.req_comment.list_by_req("REQ-2026-001")
    assert len(cms) == 2 and cms[0].origin == "feishu" and cms[0].external_id == "cm-1"
    assert cms[1].reply_to == cms[0].id


def test_finish_no_output_file_fails(env: tuple[Database, Repos, Services, Path]) -> None:
    _, repos, services, ws = env
    _mk_card(repos)
    t = services.tasks.create_guarded("REQ-2026-001", "source_import", "", "")
    services.tasks.claim("test-runner")
    out = services.tasks.finish(t.id, True, {})
    assert out.status == "failed"
    m = json.loads(out.result)
    assert m["reason"] == "import-failed"
    assert "导入输出文件不存在" in m["error"] and str(ws) in m["error"]  # 文案携带服务端生效根
    # 块表空 + 任务可 retry（失败重试合法，§2.4）
    assert repos.req_block.empty("REQ-2026-001")
    services.tasks.retry(t.id)


def test_finish_import_idempotent(env: tuple[Database, Repos, Services, Path]) -> None:
    """幂等重跑（§3）：无平台数据的卡重导 = 全量替换块 + feishu 评论，不叠加；
    （有平台评论的卡重导被封存守卫拒——见 test_import_seal_guards，两测试互补覆盖 §2.4/§3 边界。）"""
    _, repos, services, ws = env
    _mk_card(repos, source_type="feishu")
    for i in (1, 2):
        t = services.tasks.create_guarded("REQ-2026-001", "source_import", f"round-{i}", "")
        services.tasks.claim("test-runner")
        _mk_out(ws, t.id, _DOC)
        services.tasks.finish(t.id, True, {})
    blocks = repos.req_block.list_by_req("REQ-2026-001")
    assert len(blocks) == 3  # 替换不叠加
    cms = repos.req_comment.list_by_req("REQ-2026-001")
    assert len(cms) == 2 and all(c.origin == "feishu" for c in cms)  # feishu 行替换不叠加


# ---------- noDraftTypes 与 self_check 跳过 ----------

def test_task_rules_payload_carry_new_keys() -> None:
    from platform_sdlc.task_rules import task_rules_payload
    p = task_rules_payload("/ws/x")
    assert p["noDraftTypes"] == ["source_import"]
    assert p["workspaceRoot"] == "/ws/x"


def test_loop_self_check_skips_no_draft_types(env: tuple[Database, Repos, Services, Path]) -> None:
    _, repos, services, _ = env
    settings = Settings(_env_file=None)  # pyright: ignore[reportCallIssue]
    loop = AgentLoop(settings, repos, services)
    _mk_card(repos)
    t = services.tasks.create_guarded("REQ-2026-001", "source_import", "", "")
    assert loop.self_check(t) is None  # 无草稿面：跳过（不查不存在的草稿）


# ---------- 三处一致性守护纯函数 ----------

def test_check_consistency_detects_drift() -> None:
    check_consistency = _ctt.check_consistency

    base = ({"quality_check", "source_import"}, {"quality_check", "source_import"},
            {"quality_check", "source_import"},
            {"quality_check": "digest_bundle"}, {"source_import"}, {"digest_bundle"},
            {"quality_check": "digest_bundle"}, {"source_import"}, {"digest_bundle"})
    assert check_consistency(*base) == []  # 一致通过

    bad = list(base)
    bad[2] = {"quality_check"}  # ② runner 漏 source_import 分支
    assert any("①②" in p for p in check_consistency(*bad))

    bad = list(base)
    bad[1] = {"quality_check"}  # goal_prompt 漏 source_import 分支（会静默落 case_gen 兜底句）
    assert any("goal_prompt" in p for p in check_consistency(*bad))

    bad = list(base)
    bad[6] = {}  # DEFAULT_RULES 陈旧（第三写面）
    assert any("DEFAULT_RULES.draftTypes" in p for p in check_consistency(*bad))


def test_check_task_templates_main_green() -> None:
    """真文件全链（CI 第五道门同款入口）。"""
    assert _ctt.main() == 0
