"""批次3（AUDIT D7-3/D7-4/D7-1）：任务通道与运行时守卫单测。

覆盖：claim 并发互斥（双通道原子性回归网）/ 软删除服务层守卫 / retry·cancel 状态机 /
finish 的随发布作废语义 / expire_stale 按 runner 归属过滤（跨通道互杀修复）/ agent 机械自查分支。
"""

from __future__ import annotations

import concurrent.futures
import json
from pathlib import Path

import pytest

from platform_sdlc.agent.executor import _RESULT_LINE
from platform_sdlc.agent.loop import AgentLoop
from platform_sdlc.common import ApiException
from platform_sdlc.config import Settings
from platform_sdlc.db import Database
from platform_sdlc.migrations import run_migrations
from platform_sdlc.models import Requirement, Task
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


def make_req(repos: Repos, services: Services, status: str = "clarifying") -> str:
    r = Requirement(id="", name="运行时测试", iter="", source_type="text", source_text="# 原文",
                    status="submitted", submitted_by="t", standard_doc="",
                    created_at=NOW, synced_at=NOW, risk_tier="B")
    r.id = repos.requirement.next_id("2026")
    repos.requirement.insert(r)
    if status != "submitted":
        repos.requirement.set_status(r.id, "reviewing")
        repos.requirement.set_status(r.id, status)
    return r.id


# ---------- claim 并发互斥（红线回归网：AUDIT D7-3） ----------


def test_claim_concurrent_exactly_one_winner(env: tuple[Repos, Services]) -> None:
    """双通道并发领取同一队头任务：恰一方成功（条件 UPDATE 原子性），另一方 None。"""
    repos, services = env
    req_id = make_req(repos, services)
    services.tasks.create(req_id, {"type": "quality_check"})

    def try_claim(runner: str) -> Task | None:
        return services.tasks.claim(runner)

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        a = pool.submit(try_claim, "runner-a")
        b = pool.submit(try_claim, "runner-b")
        got = [a.result(), b.result()]
    winners = [t for t in got if t is not None]
    assert len(winners) == 1
    assert winners[0].runner in ("runner-a", "runner-b")
    # 队列已空：第三方领取 None
    assert services.tasks.claim("runner-c") is None


def test_claim_concurrent_many_tasks_no_double_claim(env: tuple[Repos, Services]) -> None:
    """多任务 × 多执行器并发领取：无任务被领两次、无执行器领到同一 id。"""
    repos, services = env
    # 同需求同类型活跃任务互斥（create_guarded 409），每需求一单：六需求六任务
    for _ in range(6):
        req_id = make_req(repos, services)
        services.tasks.create(req_id, {"type": "quality_check"})

    def drain(runner: str) -> list[int]:
        out: list[int] = []
        while (t := services.tasks.claim(runner)) is not None:
            out.append(t.id)
        return out

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        results = [f.result() for f in [pool.submit(drain, f"r{i}") for i in range(4)]]
    all_ids = [i for ids in results for i in ids]
    assert len(all_ids) == 6 and len(set(all_ids)) == 6  # 全部领走、无重复


# ---------- 软删除服务层守卫（AUDIT D7-4；此前仅 verify-delete.mjs 黑盒） ----------


def test_delete_rejects_running_task(env: tuple[Repos, Services]) -> None:
    """running 任务先拒删（409 提示等待）。"""
    repos, services = env
    req_id = make_req(repos, services)
    services.tasks.create(req_id, {"type": "quality_check"})
    assert services.tasks.claim("runner-x") is not None  # 置 running
    with pytest.raises(ApiException) as e:
        services.requirements.delete(req_id)
    assert e.value.status == 409 and "任务执行中" in str(e.value)


def test_delete_cancels_queued_and_soft_marks(env: tuple[Repos, Services]) -> None:
    """queued 批量取消（failed + req-deleted 作废语义）+ 行打标；已删卡再删 404。"""
    repos, services = env
    req_id = make_req(repos, services)
    services.tasks.create(req_id, {"type": "quality_check"})
    out = services.requirements.delete(req_id)
    assert out["ok"] is True and out["cancelledTasks"] == 1
    t = repos.task.list(req_id, None)[0]
    assert t.status == "failed" and "req-deleted" in t.result
    assert repos.requirement.find(req_id) is None  # 根过滤：已删对服务层 404
    with pytest.raises(ApiException) as e:
        services.requirements.delete(req_id)
    assert e.value.status == 404


# ---------- retry / cancel 状态机（AUDIT D7-4） ----------


def test_retry_and_cancel_guards(env: tuple[Repos, Services]) -> None:
    repos, services = env
    req_id = make_req(repos, services)
    t = services.tasks.create(req_id, {"type": "quality_check"})

    with pytest.raises(ApiException) as e:  # queued 不可 retry
        services.tasks.retry(t.id)
    assert e.value.status == 409

    cancelled = services.tasks.cancel(t.id)
    assert cancelled.status == "failed"
    with pytest.raises(ApiException) as e:  # 已 failed 不可再 cancel
        services.tasks.cancel(t.id)
    assert e.value.status == 409

    retried = services.tasks.retry(t.id)
    assert retried.status == "queued" and retried.runner == ""


def test_retry_rejects_superseded_by_publish(env: tuple[Repos, Services]) -> None:
    """已发布需求的质检类失败任务：随发布作废，不可重试（人取消即终局的红线延伸）。"""
    repos, services = env
    req_id = make_req(repos, services, status="published")
    t = services.tasks.create(req_id, {"type": "case_gen"})  # 发布后合法类型，可正常建
    assert services.tasks.claim("runner-x") is not None
    services.tasks.finish(t.id, False, {"error": "x"})
    # 造一条发布前遗留的质检 failed：直接落库（create 在 published 下会拒 quality_check）
    stale = Task(id=0, req_id=req_id, type="quality_check", status="failed",
                 result='{"error":"old"}', runner="r", created_by="", created_at=NOW, updated_at=NOW)
    stale.id = repos.task.insert(stale)
    with pytest.raises(ApiException) as e:
        services.tasks.retry(stale.id)
    assert e.value.status == 409 and "作废" in str(e.value)


def test_finish_failed_superseded_gets_reason(env: tuple[Repos, Services]) -> None:
    """外置通道对已发布需求的失败回报：统一补 superseded-by-publish 作废语义（评测 P0-2b）。"""
    repos, services = env
    req_id = make_req(repos, services, status="published")
    t = Task(id=0, req_id=req_id, type="quality_check", status="running",
             result="", runner="r", created_by="", created_at=NOW, updated_at=NOW)
    t.id = repos.task.insert(t)
    out = services.tasks.finish(t.id, False, {})
    assert out.status == "failed"
    body = json.loads(out.result)
    assert body["reason"] == "superseded-by-publish" and body["hint"]


# ---------- expire_stale 按 runner 归属过滤（批次2 互杀修复的回归网） ----------


def test_expire_stale_filters_by_runner(env: tuple[Repos, Services]) -> None:
    """超时恢复只清指定执行器名下任务：另一通道正在执行的任务不得被误判死。"""
    repos, services = env
    req_id = make_req(repos, services)
    old = "2026-10-01 08:00:00"  # 超出 30 分钟阈值
    for runner in ("runner-a", "runner-b", "openai-agents-inproc"):
        t = Task(id=0, req_id=req_id, type="quality_check", status="running",
                 result="", runner=runner, created_by="", created_at=old, updated_at=old)
        t.id = repos.task.insert(t)
        # TaskRepo.insert 的 runner 列为常量 ''（业务上 runner 只由 claim 写入）——直改列模拟执行中归属
        repos.task.db.execute("UPDATE task SET runner=?, updated_at=? WHERE id=?", (runner, old, t.id))

    n = services.tasks.expire_stale(30, runner="runner-a")
    assert n == 1
    remaining = {t.runner: t.status for t in repos.task.list(req_id, "running")}
    assert remaining == {"runner-b": "running", "openai-agents-inproc": "running"}

    # 不带归属（全队列）仍是服务端自愈的兜底口径
    n = services.tasks.expire_stale(30)
    assert n == 2


# ---------- agent 机械自查（AUDIT D7-1：agent 通道补回归网） ----------


def _loop(env: tuple[Repos, Services]) -> AgentLoop:
    repos, services = env
    return AgentLoop(Settings(), repos, services)


def test_agent_self_check_branches(env: tuple[Repos, Services]) -> None:
    """self_check 六分支：未回传 / 过短 / 缺结构标头 / issues 空 / contentJson 不完整 / 通过。"""
    repos, services = env
    loop = _loop(env)
    req_id = make_req(repos, services)

    def task_of(type_: str) -> Task:
        return Task(id=0, req_id=req_id, type=type_, status="running", result="", runner="",
                    created_by="", created_at=NOW, updated_at=NOW)

    # 1) 草稿未回传
    assert "草稿未回传" in (loop.self_check(task_of("standard_doc")) or "")

    # 2) standard 过短
    services.tasks.put_draft(req_id, "standard", {"md": "# 太短"})
    assert "过短" in (loop.self_check(task_of("standard_doc")) or "")

    # 3) design 缺 §0 九节标头（长度够但无结构）
    services.tasks.put_draft(req_id, "design", {"md": "# 设计文档\n" + "正文内容。" * 100})
    assert "§0" in (loop.self_check(task_of("design_gen")) or "")

    # 4) gate_issues 的 contentJson.issues 为空
    services.tasks.put_draft(req_id, "gate_issues",
                             {"md": "# 评审问题\n" + "内容。" * 50, "contentJson": '{"issues":[]}'})
    assert "issues 为空" in (loop.self_check(task_of("gate_review")) or "")

    # 5) digest_bundle 结构不完整（缺 items）
    services.tasks.put_draft(req_id, "digest_bundle",
                             {"contentJson": '{"digest":{"md":"# d","risk_tier":"B"},"audit":{"md":"# a"}}'})
    assert "结构不完整" in (loop.self_check(task_of("quality_check")) or "")

    # 6) 合法草稿 → None（放行 done）
    services.tasks.put_draft(req_id, "digest_bundle", {"contentJson": json.dumps({
        "digest": {"md": "# d", "risk_tier": "B", "fr_points": []},
        "audit": {"md": "# a", "red": 0, "yellow": 0, "blue": 0, "items": []},
        "items": [{"seq": "P01", "level": "yellow", "theme": "t", "question": "q?"},
                  {"seq": "P02", "level": "yellow", "theme": "t", "question": "q2?"}]})})
    assert loop.self_check(task_of("quality_check")) is None


def test_result_line_regex() -> None:
    """结果行判据（双通道 done 判定对齐的共享语义）：仅收尾行生效、容忍空白、拒绝非 JSON 尾巴。"""
    m = _RESULT_LINE.search("前置说明\nSDLC_TASK_RESULT {\"draft\":\"standard\"}")
    assert m is not None and json.loads(m.group(1)) == {"draft": "standard"}
    assert _RESULT_LINE.search("正文无结果行") is None
