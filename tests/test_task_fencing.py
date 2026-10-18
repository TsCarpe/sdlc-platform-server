"""P1-6 / N-1 回归：任务终态 fencing（attempt）与 source_import 副作用顺序。

覆盖（2026-10 三仓评估批 B）：
- 先校验后副作用：非 running / 执行器不符 / 旧 attempt 的 done·failed 一律 409，块表/评论零变化；
- 重复 done 不重复导入；retry 后旧 attempt 的迟到回报不改变新 attempt；
- source_import 导入前复检封存条件（建任务后平台编辑）与需求存活（软删）；
- retry reason 白名单：生命周期取消/作废终局；claim 不领取软删需求的任务；
- 进程内 agent 命令子进程的环境白名单（P1-10）。
"""

from __future__ import annotations

import asyncio
import copy
import json
import subprocess
from pathlib import Path
from typing import Any, cast

import pytest
from agents import RunContextWrapper
from fastapi.testclient import TestClient

from platform_sdlc.agent import tools
from platform_sdlc.agent.context import TaskRunContext
from platform_sdlc.common import ApiException, is_json_obj
from platform_sdlc.config import Settings
from platform_sdlc.db import Database
from platform_sdlc.main import create_app
from platform_sdlc.migrations import run_migrations
from platform_sdlc.models import ReqBlock, Requirement, Task
from platform_sdlc.repositories import Repos
from platform_sdlc.services import Services
from platform_sdlc.services.block import doc_text_stream, normalize_text_fingerprint

NOW = "2026-10-01 10:00:00"

_DOC = {
    "blocks": [
        {"id": "f-h1", "type": "heading", "level": 1, "text": "背景"},
        {"id": "f-p1", "type": "paragraph", "text": "第一段"},
    ],
    "media": [],
}
_DOC_ALT = {
    "blocks": [
        {"id": "f-h1", "type": "heading", "level": 1, "text": "背景"},
        {"id": "f-p1", "type": "paragraph", "text": "第二版"},
    ],
    "media": [],
}


def _with_guard(doc: dict[str, Any]) -> dict[str, Any]:
    """合法守恒 guard（与 test_v4_import 同款现算，不硬编码）。"""
    d = copy.deepcopy(doc)
    media: list[Any] = d.get("media") or []
    tokens = sorted({str(m["token"]) for m in media if is_json_obj(m) and m["token"]})
    d["guard"] = {"algo": "conservation-v1", "source": "xml", "mediaTokens": tokens,
                  "textSha": normalize_text_fingerprint(doc_text_stream(d["blocks"]))}
    return d


@pytest.fixture()
def env(tmp_path: Path) -> tuple[Repos, Services, Path]:
    db = Database(tmp_path / "platform.db")
    run_migrations(db)
    repos = Repos.create(db)
    services = Services.create(db, repos, tmp_path / "media", tmp_path)
    return repos, services, tmp_path


def _mk_card(repos: Repos, req_id: str = "REQ-2026-001", source_type: str = "text") -> str:
    repos.requirement.insert(Requirement(
        id=req_id, name="fencing 验证", source_type=source_type,
        source_url="https://feishu.example/x" if source_type == "feishu" else "",
        source_text="# 原文" if source_type == "text" else "",
        status="submitted", submitted_by="t"))
    return req_id


def _mk_out(ws: Path, task_id: int, doc: dict[str, Any]) -> None:
    out = ws / "tasks" / str(task_id) / "out"
    out.mkdir(parents=True, exist_ok=True)
    (out / "doc.json").write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")


# ---------- attempt fencing（P1-6） ----------

def test_attempt_fencing_blocks_late_callbacks(env: tuple[Repos, Services, Path]) -> None:
    """retry 后旧执行器的迟到 done/failed：attempt 不匹配 → 409，不改变新 attempt 的 running。"""
    repos, services, _ = env
    req_id = _mk_card(repos)
    t = services.tasks.create_guarded(req_id, "case_gen", "", "")
    claimed = services.tasks.claim("runner-a")
    assert claimed is not None and claimed.id == t.id and claimed.attempt == 0
    services.tasks.finish(t.id, False, {"error": "boom", "reason": "agent-exception",
                                        "runner": "runner-a", "attempt": 0})
    retried = services.tasks.retry(t.id)
    assert retried.status == "queued" and retried.attempt == 1
    again = services.tasks.claim("runner-b")
    assert again is not None and again.id == t.id and again.attempt == 1

    # 用当前归属 runner-b 回报旧 attempt=0：隔离出 attempt fencing（runner 不符另有用例覆盖）
    for ok, body in ((True, {"result": "late"}), (False, {"error": "late"})):
        with pytest.raises(ApiException) as e:
            services.tasks.finish(t.id, ok, {**body, "runner": "runner-b", "attempt": 0})
        assert e.value.status == 409 and "attempt" in str(e.value)
    current = repos.task.find(t.id)
    assert current is not None and current.status == "running" and current.attempt == 1

    out = services.tasks.finish(t.id, True, {"result": "ok", "runner": "runner-b", "attempt": 1})
    assert out.status == "done"


def test_runner_mismatch_rejected(env: tuple[Repos, Services, Path]) -> None:
    """执行器归属不符：另一通道的回报不得改写本通道任务（runner 可选，传了就必须匹配）。"""
    repos, services, _ = env
    req_id = _mk_card(repos)
    t = services.tasks.create_guarded(req_id, "case_gen", "", "")
    services.tasks.claim("runner-a")
    with pytest.raises(ApiException) as e:
        services.tasks.finish(t.id, True, {"result": "x", "runner": "runner-b", "attempt": 0})
    assert e.value.status == 409 and "执行器不匹配" in str(e.value)
    assert repos.task.find(t.id).status == "running"  # type: ignore[union-attr]


def test_repeated_done_no_second_import(env: tuple[Repos, Services, Path]) -> None:
    """重复 done：第二次 409 且不重复导入（先用输出文件换成另一版验证块表零变化）。"""
    repos, services, ws = env
    req_id = _mk_card(repos)
    t = services.tasks.create_guarded(req_id, "source_import", "", "")
    services.tasks.claim("runner-a")
    _mk_out(ws, t.id, _with_guard(_DOC))
    first = services.tasks.finish(t.id, True, {"runner": "runner-a", "attempt": t.attempt})
    assert first.status == "done"
    assert [b.text for b in repos.req_block.list_by_req(req_id)] == ["背景", "第一段"]

    _mk_out(ws, t.id, _with_guard(_DOC_ALT))
    with pytest.raises(ApiException) as e:
        services.tasks.finish(t.id, True, {"runner": "runner-a", "attempt": t.attempt})
    assert e.value.status == 409
    assert [b.text for b in repos.req_block.list_by_req(req_id)] == ["背景", "第一段"]


# ---------- source_import 导入前复检（P1-6③ / N-1） ----------

def test_import_sealed_after_platform_edit(env: tuple[Repos, Services, Path]) -> None:
    """建任务后块表出现平台编辑 → 完成回调的导入前复检拒绝（reason=sealed），平台块保持原状。"""
    repos, services, ws = env
    req_id = _mk_card(repos)
    t = services.tasks.create_guarded(req_id, "source_import", "", "")
    services.tasks.claim("runner-a")
    repos.req_block.replace_all(req_id, [
        ReqBlock(req_id=req_id, block_id="blk-platform", seq=1000, type="paragraph", text="平台编辑")])
    _mk_out(ws, t.id, _with_guard(_DOC))

    out = services.tasks.finish(t.id, True, {"runner": "runner-a", "attempt": 0})
    assert out.status == "failed"
    m = json.loads(out.result)
    assert m["reason"] == "sealed" and "封存" in m["error"]
    assert [b.text for b in repos.req_block.list_by_req(req_id)] == ["平台编辑"]


def test_import_rejected_after_requirement_deleted(env: tuple[Repos, Services, Path]) -> None:
    """领取后需求被软删（竞态模拟）：完成回调不落库，任务记 req-deleted 终态。"""
    repos, services, ws = env
    req_id = _mk_card(repos)
    t = services.tasks.create_guarded(req_id, "source_import", "", "")
    services.tasks.claim("runner-a")
    repos.requirement.soft_delete(req_id, NOW)
    _mk_out(ws, t.id, _with_guard(_DOC))

    out = services.tasks.finish(t.id, True, {"runner": "runner-a", "attempt": 0})
    assert out.status == "failed"
    m = json.loads(out.result)
    assert m["reason"] == "req-deleted"
    assert repos.req_block.list_by_req(req_id) == []


def test_retry_rejected_after_requirement_deleted(env: tuple[Repos, Services, Path]) -> None:
    """软删需求的任务：即便 reason 可重试，retry 也 409（需求存活复检先于白名单）。"""
    repos, services, _ = env
    req_id = _mk_card(repos)
    t = Task(id=0, req_id=req_id, type="case_gen", status="failed",
             result=json.dumps({"error": "x", "reason": "timeout"}, ensure_ascii=False),
             runner="r", created_by="", created_at=NOW, updated_at=NOW)
    tid = repos.task.insert(t)
    repos.requirement.soft_delete(req_id, NOW)
    with pytest.raises(ApiException) as e:
        services.tasks.retry(tid)
    assert e.value.status == 409 and "需求已删除" in str(e.value)
    assert repos.task.find(tid).status == "failed"  # type: ignore[union-attr]


def test_claim_skips_deleted_requirement(env: tuple[Repos, Services, Path]) -> None:
    """软删需求残留的 queued 任务不领取（服务层删除已批量取消；此处堵竞态/直改库残留）。"""
    repos, services, _ = env
    req_id = _mk_card(repos)
    services.tasks.create_guarded(req_id, "case_gen", "", "")
    repos.requirement.soft_delete(req_id, NOW)
    assert services.tasks.claim("runner-a") is None


def test_retry_reason_whitelist(env: tuple[Repos, Services, Path]) -> None:
    """可重试 reasons 放行；生命周期取消/作废/未知 reason 一律 409（人取消即终局）。"""
    repos, services, _ = env
    req_id = _mk_card(repos)

    def failed_task(reason: str) -> int:
        t = Task(id=0, req_id=req_id, type="case_gen", status="failed",
                 result=json.dumps({"error": "x", "reason": reason}, ensure_ascii=False),
                 runner="r", created_by="", created_at=NOW, updated_at=NOW)
        return repos.task.insert(t)

    for reason in ("import-failed", "integrity-failed", "timeout", "no-draft",
                   "agent-exception", "blocks-empty"):
        tid = failed_task(reason)
        assert services.tasks.retry(tid).status == "queued"
        assert repos.task.find(tid).attempt == 1  # type: ignore[union-attr]

    for reason in ("req-deleted", "change-aborted", "cancelled", "sealed", "unknown-type", ""):
        tid = failed_task(reason)
        with pytest.raises(ApiException) as e:
            services.tasks.retry(tid)
        assert e.value.status == 409


# ---------- recover-stale 归属语义（P1-5） ----------

def test_recover_stale_requires_runner_or_all(tmp_path: Path) -> None:
    """裸调用拒绝（400）；按 runner 或显式 all=true 才执行全量/归属恢复。"""
    settings = Settings(_env_file=None, db_path=str(tmp_path / "platform.db"),  # pyright: ignore[reportCallIssue]
                        media_dir=str(tmp_path / "media"))
    app = create_app(settings, cwd=tmp_path)
    with TestClient(app) as c:
        assert c.post("/api/tasks/recover-stale").status_code == 400
        r = c.post("/api/tasks/recover-stale", params={"runner": "r-x"})
        assert r.status_code == 200 and r.json() == {"ok": True, "expired": 0}
        r = c.post("/api/tasks/recover-stale", params={"all": "true"})
        assert r.status_code == 200 and r.json() == {"ok": True, "expired": 0}


# ---------- P1-10：命令子进程环境白名单 ----------

def test_child_env_excludes_sensitive_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SDLC_WRITE_TOKEN", "secret")
    monkeypatch.setenv("SDLC_AGENT_API_KEY", "agent-key")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-anthropic")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "aws")
    monkeypatch.setenv("LARK_APP_SECRET", "lark")
    env = tools._child_env()
    for key in ("SDLC_WRITE_TOKEN", "SDLC_AGENT_API_KEY", "OPENAI_API_KEY",
                "ANTHROPIC_API_KEY", "AWS_SECRET_ACCESS_KEY", "LARK_APP_SECRET"):
        assert key not in env
    assert "PATH" in env


def test_run_command_passes_whitelisted_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """mock subprocess.run：实际传入的 env 是白名单且不含敏感键；cwd=任务工作区。"""
    captured: dict[str, Any] = {}

    def fake_run(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        captured.update(kwargs)
        return subprocess.CompletedProcess(args, 0, stdout="ok", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setenv("SDLC_WRITE_TOKEN", "secret")
    ctx = TaskRunContext(req_id="R", task_id=1, task_type="quality_check", workspace=tmp_path,
                         skills_repo=tmp_path, services=cast(Any, None), repos=cast(Any, None))
    out = asyncio.run(tools.run_command(RunContextWrapper(context=ctx), "ls"))
    assert json.loads(out)["exit"] == 0
    assert captured["cwd"] == tmp_path
    env = cast(dict[str, str], captured["env"])
    assert "SDLC_WRITE_TOKEN" not in env and "PATH" in env
