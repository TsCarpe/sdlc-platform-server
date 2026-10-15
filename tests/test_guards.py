"""P2 验收：业务红线守卫单测（逐条对照 CLAUDE.md「业务红线」）。

覆盖：重推保留答复 / P 编号漂移防护 / 发布终态锁 / 状态机守卫 / 澄清处置语义 /
用例乐观锁 conflicts / 全量替换保留表单用例 / 编辑权守卫 / L2 编排只自动一次 / 门禁放行互认。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

import pytest

from platform_sdlc.common import ApiException, ConflictException
from platform_sdlc.db import Database
from platform_sdlc.migrations import run_migrations
from platform_sdlc.models import ClarifyItem, DraftArtifact, FrPoint, MediaFile, Requirement, Task
from platform_sdlc.repositories import Repos
from platform_sdlc.services import Services

NOW = "2026-10-01 10:00:00"


@pytest.fixture()
def env(tmp_path: Path) -> tuple[Repos, Services]:
    db = Database(tmp_path / "platform.db")
    run_migrations(db)
    repos = Repos.create(db)
    services = Services.create(db, repos, tmp_path / "media", tmp_path)
    return repos, services


def make_req(repos: Repos, services: Services, status: str = "clarifying", tier: str = "B") -> str:
    r = Requirement(id="", name="测试需求", iter="", source_type="text", source_text="# 原文",
                    status="submitted", submitted_by="t", standard_doc="",
                    created_at=NOW, synced_at=NOW, risk_tier=tier)
    r.id = repos.requirement.next_id("2026")  # 走正规取号（用例域入口校验 REQ 编号格式）
    repos.requirement.insert(r)
    if status != "submitted":
        repos.requirement.set_status(r.id, "reviewing")
        repos.requirement.set_status(r.id, status)
    return r.id


# ---------- 需求域红线 ----------


def test_push_artifacts_keeps_answers(env: tuple[Repos, Services]) -> None:
    """红线：重推产物按 seq+问题文本保留平台已答内容；问题文本已变（P 编号漂移）不携带。"""
    repos, services = env
    req_id = make_req(repos, services)
    payload: dict[str, Any] = {"digest": {"md": "# d", "risk_tier": "B", "fr_points": [
        {"id": "需求.FR-01", "name": "登录"}]},
        "audit": {"md": "# a", "red": 0, "yellow": 1, "blue": 0, "items": []},
        "items": [
            {"seq": "P01", "level": "red", "theme": "登录", "question": "密码规则？"},
            {"seq": "P02", "level": "yellow", "theme": "登录", "question": "验证码？"},
        ]}
    out1 = services.requirements.push_artifacts(req_id, payload)
    assert out1["items"] == 2 and out1["carriedAnswers"] == 0

    services.requirements.set_answer(req_id, "P01", {"answer": "至少 8 位"})
    services.requirements.set_answer(req_id, "P02", {"answer": "6 位"})
    # 重推：P01 同文保留答复；P02 已处置且问题文本已变 → 漂移不携带（pending 纯净行不参与漂移检测）
    payload2: dict[str, Any] = {**payload, "items": [
        {"seq": "P01", "level": "red", "theme": "登录", "question": "密码规则？"},
        {"seq": "P02", "level": "yellow", "theme": "登录", "question": "验证码改成多少位？"},
    ]}
    out2 = services.requirements.push_artifacts(req_id, payload2)
    assert out2["carriedAnswers"] == 1 and out2["changedQuestions"] == 1
    answers = {a["seq"]: a for a in services.requirements.answers(req_id)}
    assert answers["P01"]["answer"] == "至少 8 位" and answers["P01"]["status"] == "answered"
    assert answers["P02"]["answer"] == "" and answers["P02"]["status"] == "pending"


def test_push_artifacts_platform_disposition_wins(env: tuple[Repos, Services]) -> None:
    """红线（DESIGN-v5 B5/D17）：平台已处置（驳回/挂起）一律以平台为准携带——skill 清单自带
    answer（pull-answers 回写后重推的常态）不得把平台处置顶回 answered。"""
    repos, services = env
    req_id = make_req(repos, services)
    payload: dict[str, Any] = {"digest": {"md": "# d", "fr_points": []},
                               "audit": {"md": "# a", "red": 0, "yellow": 1, "blue": 0, "items": []},
                               "items": [
                                   {"seq": "P01", "level": "red", "theme": "登录", "question": "密码规则？"},
                                   {"seq": "P02", "level": "yellow", "theme": "登录", "question": "验证码？"},
                               ]}
    services.requirements.push_artifacts(req_id, payload)
    services.requirements.set_answer(req_id, "P01", {"status": "rejected", "note": "原文已明确"})
    services.requirements.set_answer(req_id, "P02", {"status": "deferred", "note": "下期再议"})
    # 重推清单带 answer（skill 侧已被回写答复）——平台 rejected/deferred 不得被覆盖
    payload2: dict[str, Any] = {**payload, "items": [
        {"seq": "P01", "level": "red", "theme": "登录", "question": "密码规则？", "answer": "旧答复文本"},
        {"seq": "P02", "level": "yellow", "theme": "登录", "question": "验证码？", "answer": "旧答复文本"},
    ]}
    out = services.requirements.push_artifacts(req_id, payload2)
    assert out["carriedAnswers"] == 2
    answers = {a["seq"]: a for a in services.requirements.answers(req_id)}
    assert answers["P01"]["status"] == "rejected" and answers["P01"]["note"] == "原文已明确"
    assert answers["P01"]["answer"] == ""   # 驳回行平台 answer 为空——不被 skill 自带 answer 顶入
    assert answers["P02"]["status"] == "deferred" and answers["P02"]["note"] == "下期再议"


def test_push_artifacts_carries_reopened_note(env: tuple[Repos, Services]) -> None:
    """红线（DESIGN-v5 D17/r3 二审#5）：重开行（answer=''、status='pending'、note=变更单重开备注）
    必须进携带 map——否则重推丢 note 且 skill 自带 answer 会把重开态顶回 answered。"""
    repos, services = env
    req_id = make_req(repos, services)
    payload: dict[str, Any] = {"digest": {"md": "# d", "fr_points": []},
                               "audit": {"md": "# a", "red": 0, "yellow": 0, "blue": 0, "items": []},
                               "items": [
                                   {"seq": "P01", "level": "red", "theme": "登录", "question": "密码规则？"},
                               ]}
    services.requirements.push_artifacts(req_id, payload)
    services.requirements.set_answer(req_id, "P01", {"answer": "8 位", "status": "answered"})
    # 变更单重开走 change.py 真实通道（reopen_items：answer 清空回 pending，note 带原答复备注）——
    # 注意与 set_answer 的重开不同（其会从库回填 answer），真实重开行 answer 确实为空
    repos.clarify_item.reopen_items(
        req_id, [("P01", "因变更单#1 重开，原答复：8 位", None)])
    payload2: dict[str, Any] = {**payload, "items": [
        {"seq": "P01", "level": "red", "theme": "登录", "question": "密码规则？", "answer": "8 位"},
    ]}
    out = services.requirements.push_artifacts(req_id, payload2)
    assert out["carriedAnswers"] == 1
    row = {a["seq"]: a for a in services.requirements.answers(req_id)}["P01"]
    assert row["status"] == "pending" and row["note"] == "因变更单#1 重开，原答复：8 位"
    assert row["answer"] == ""   # 重开行保持待处置——skill 自带 answer 不生效


def test_published_terminal_lock(env: tuple[Repos, Services]) -> None:
    """红线：发布后锁定——拒推产物、拒写质检草稿、拒建质检任务、拒作答。"""
    repos, services = env
    req_id = make_req(repos, services)
    repos.clarify_item.insert(ClarifyItem(
        id=0, req_id=req_id, seq="P01", level="red", theme="t", question="q", answer="a", status="answered"))
    services.requirements.publish(req_id, {"standardMd": "# 标准文档"})

    push_again = {"digest": {"md": "x"}, "audit": {"md": "y"},
                  "items": [{"seq": "P01", "level": "red", "theme": "t", "question": "q"}]}
    with pytest.raises(ApiException) as e1:
        services.requirements.push_artifacts(req_id, push_again)
    assert e1.value.status == 409
    with pytest.raises(ApiException) as e2:
        services.tasks.put_draft(req_id, "digest_bundle", {"contentJson": "{}"})
    assert e2.value.status == 409
    with pytest.raises(ApiException) as e3:
        services.tasks.create(req_id, {"type": "quality_check"})
    assert e3.value.status == 409
    with pytest.raises(ApiException) as e4:
        services.requirements.set_answer(req_id, "P01", {"answer": "再答"})
    assert e4.value.status == 409
    # 发布后合法：case_gen 任务与 cases 草稿不受终态锁约束（D36）
    assert services.tasks.create(req_id, {"type": "case_gen"}).status == "queued"


def test_status_machine_guard(env: tuple[Repos, Services]) -> None:
    """红线：状态机由 skill 同步写入——仅允许 submitted→reviewing→clarifying。"""
    repos, services = env
    req_id = make_req(repos, services, status="submitted")
    with pytest.raises(ApiException) as e:
        services.requirements.set_status(req_id, {"status": "clarifying"})  # 跳级
    assert e.value.status == 409
    services.requirements.set_status(req_id, {"status": "reviewing"})
    services.requirements.set_status(req_id, {"status": "clarifying"})
    with pytest.raises(ApiException) as e2:
        services.requirements.set_status(req_id, {"status": "reviewing"})  # 不可逆
    assert e2.value.status == 409


def test_set_answer_disposition_semantics(env: tuple[Repos, Services]) -> None:
    """红线：rejected 必带 note；rejected/deferred 保留半截答复；显式重开回 pending。"""
    repos, services = env
    req_id = make_req(repos, services)
    services.requirements.push_artifacts(req_id, {
        "digest": {"md": "# d"}, "audit": {"md": "# a"},
        "items": [{"seq": "P01", "level": "red", "theme": "t", "question": "q", "answer": "半截答复"}]})
    with pytest.raises(ApiException) as e1:
        services.requirements.set_answer(req_id, "P01", {"status": "rejected"})
    assert "驳回必须填写理由" in str(e1.value)
    services.requirements.set_answer(req_id, "P01", {"status": "rejected", "note": "误报"})
    a = services.requirements.answers(req_id)[0]
    assert a["status"] == "rejected" and a["answer"] == "半截答复"  # 半截答复不清洗（D26）
    services.requirements.set_answer(req_id, "P01", {"status": "pending"})
    assert services.requirements.answers(req_id)[0]["status"] == "pending"
    services.requirements.set_answer(req_id, "P01", {"answer": "正式答复"})
    assert services.requirements.answers(req_id)[0]["status"] == "answered"


def test_publish_requires_all_disposed(env: tuple[Repos, Services]) -> None:
    repos, services = env
    req_id = make_req(repos, services)
    services.requirements.push_artifacts(req_id, {
        "digest": {"md": "# d"}, "audit": {"md": "# a"},
        "items": [{"seq": "P01", "level": "red", "theme": "t", "question": "q"}]})
    with pytest.raises(ApiException) as e:
        services.requirements.publish(req_id, {"standardMd": "# s"})
    assert "存在未处置问题（0/1）" in str(e.value)
    services.requirements.set_answer(req_id, "P01", {"status": "deferred", "note": "挂起"})
    out = services.requirements.publish(req_id, {"standardMd": "# s"})
    assert out["ok"] is True
    assert repos.requirement.find(req_id).status == "published"  # type: ignore[union-attr]


# ---------- 用例域红线 ----------


def _sync_body(req_id: str, items: list[dict[str, Any]]) -> dict[str, Any]:
    return {"reqId": req_id, "items": items}


def test_case_optimistic_lock_conflicts(env: tuple[Repos, Services]) -> None:
    """红线：用例 upsert 乐观锁——expectedVersion 不符整批拒绝 409 + conflicts 明细。"""
    repos, services = env
    req_id = make_req(repos, services)
    out = services.cases.sync_upsert(None, _sync_body(req_id, [
        {"id": "TC-1", "title": "用例一", "expectedVersion": -1}]))
    assert out["inserted"] == 1
    services.cases.sync_upsert(None, _sync_body(req_id, [  # 合法更新一轮 → 服务端 v2
        {"id": "TC-1", "title": "用例一改", "expectedVersion": 1}]))
    with pytest.raises(ConflictException) as e:
        services.cases.sync_upsert(None, _sync_body(req_id, [
            {"id": "TC-1", "title": "再改", "expectedVersion": 1}]))
    assert e.value.conflicts == [{"id": "TC-1", "currentVersion": 2, "expectedVersion": 1}]


def test_case_full_replace_keeps_form_cases(env: tuple[Repos, Services]) -> None:
    """红线：全量替换只删 skill 来源用例；表单来源一律保留。"""
    repos, services = env
    req_id = make_req(repos, services)
    services.cases.sync_upsert(None, _sync_body(req_id, [
        {"id": "TC-1", "title": "skill 用例"}, {"id": "TC-2", "title": "skill 用例二"}]))
    services.cases.form_create(None, {"reqId": req_id, "title": "表单用例", "steps": "s", "expect": "e", "owner": "pm"})
    out = services.cases.sync_upsert(None, _sync_body(req_id, [{"id": "TC-1", "title": "skill 用例"}]))
    assert out["removed"] == 1 and out["removedIds"] == ["TC-2"]
    ids = [c["id"] for c in services.cases.list(None, req_id, None, None, None)]
    assert ids == ["TC-1", "WEB-001"]  # skill 用例保留 + 表单用例保留


def test_case_readonly_and_version_guard(env: tuple[Repos, Services]) -> None:
    """红线：tool='sdlc-test' 用例对平台只读+评论；表单编辑版本冲突 409。"""
    repos, services = env
    req_id = make_req(repos, services)
    services.cases.sync_upsert(None, _sync_body(req_id, [{"id": "TC-1", "title": "t", "owner": "robot"}]))
    with pytest.raises(ApiException) as e1:
        services.cases.form_update(req_id, "TC-1", {"title": "改"})
    assert "平台只读" in str(e1.value)
    services.cases.add_comment(req_id, "TC-1", {"by": "pm", "text": "评论可以"})  # 评论通道畅通

    web = services.cases.form_create(None, {"reqId": req_id, "title": "t2", "steps": "s", "expect": "e", "owner": "pm"})
    with pytest.raises(ApiException) as e2:
        services.cases.form_update(req_id, "WEB-001", {"title": "改", "expectedVersion": 99})
    assert "版本已变化" in str(e2.value)
    detail = services.cases.form_update(req_id, "WEB-001", {"title": "改", "expectedVersion": web["version"]})
    assert detail["title"] == "改" and detail["version"] == 2


def test_priority_strict_form_lenient_skill(env: tuple[Repos, Services]) -> None:
    """红线：表单通道非法 priority 400；skill 通道宽容归一 P2 并计数。"""
    repos, services = env
    req_id = make_req(repos, services)
    with pytest.raises(ApiException):
        services.cases.form_create(None, {"reqId": req_id, "title": "t", "steps": "s", "expect": "e",
                                          "owner": "pm", "priority": "P5"})
    out = services.cases.sync_upsert(None, _sync_body(req_id, [{"id": "TC-1", "title": "t", "priority": "P5"}]))
    assert out["normalizedPriority"] == 1


# ---------- 任务域与 L2 编排 ----------


def test_l2_only_once_and_settle(env: tuple[Repos, Services]) -> None:
    """红线：L2 只自动一次（人取消即终局）；发布收尾取消 queued 残留、不复活。"""
    repos, services = env
    req_id = make_req(repos, services)
    services.requirements.push_artifacts(req_id, {
        "digest": {"md": "# d"}, "audit": {"md": "# a"},
        "items": [{"seq": "P01", "level": "red", "theme": "t", "question": "q"}]})
    services.requirements.set_answer(req_id, "P01", {"answer": "a"})  # 全部答完 → 边界跃迁

    services.orchestration.on_answered(req_id)
    auto_task = [t for t in repos.task.list(req_id, None) if t.type == "standard_doc"]
    assert len(auto_task) == 1 and auto_task[0].created_by == "auto:clarify-complete"
    repos.task.cancel_queued(auto_task[0].id, "{}", NOW)  # 人取消 = 终局
    services.orchestration.on_answered(req_id)  # 不复活
    assert [t for t in repos.task.list(req_id, None) if t.type == "standard_doc" and t.status == "queued"] == []

    services.requirements.publish(req_id, {"standardMd": "# s"})
    services.orchestration.on_published(req_id)
    # 质检/合成残留被收尾取消（本例无），case_gen 自动排队
    case_tasks = [t for t in repos.task.list(req_id, None) if t.type == "case_gen"]
    assert len(case_tasks) == 1 and case_tasks[0].created_by == "auto:published"
    services.orchestration.on_published(req_id)  # 只自动一次
    assert len([t for t in repos.task.list(req_id, None) if t.type == "case_gen"]) == 1


def test_l2_case_gen_windowed_after_rechange(env: tuple[Repos, Services]) -> None:
    """P3-5 回归：case_gen「只自动一次」窗口化（v4-15 与 standard_doc 同款口径）——首发排过
    （含被取消）后，变更轮（存在晚于首发任务的 confirmed 变更单）重新发布 → 自动排用例重新
    获得机会（重新发布后新增 FR 不再漏排；人工通道不受影响）。"""
    repos, services = env
    req_id = make_req(repos, services)
    services.requirements.push_artifacts(req_id, {
        "digest": {"md": "# d"}, "audit": {"md": "# a"},
        "items": [{"seq": "P01", "level": "red", "theme": "t", "question": "q"}]})
    services.requirements.set_answer(req_id, "P01", {"answer": "a"})
    services.orchestration.on_answered(req_id)
    services.requirements.publish(req_id, {"standardMd": "# s"})
    services.orchestration.on_published(req_id)
    first = [t for t in repos.task.list(req_id, None) if t.type == "case_gen"]
    assert len(first) == 1
    repos.task.cancel_queued(first[0].id, "{}", NOW)  # 首发用例任务被取消（历史终局）

    # 变更轮：晚于首发任务的 confirmed 变更单开新窗口 → 重新自动排
    from platform_sdlc.models import ReqChange
    repos.req_change.insert(ReqChange(
        id=None, req_id=req_id, kind="change", title="口径变更", detail="",
        proposed_by="张三", confirmed_by="李四", status="confirmed", blocks_json="[]",
        created_at="2999-01-01 00:00:00", confirmed_at="2999-01-01 00:00:01"))
    services.orchestration.on_published(req_id)
    cgs = [t for t in repos.task.list(req_id, None) if t.type == "case_gen"]
    assert len(cgs) == 2  # 新窗口重排（修复前：全史判断 → 永不重排）


def test_settle_terminal_cancels_queued(env: tuple[Repos, Services]) -> None:
    """发布收尾：queued 的质检任务作废（superseded-by-publish），running 不动。"""
    repos, services = env
    req_id = make_req(repos, services)
    t1 = repos.task.insert(Task(id=0, req_id=req_id, type="quality_check", created_at=NOW, updated_at=NOW))
    t2 = repos.task.insert(Task(id=0, req_id=req_id, type="standard_doc", created_at=NOW, updated_at=NOW))
    repos.task.claim("runner-x", NOW)  # t1 → running（先入队先领）
    cancelled = services.tasks.settle_terminal(req_id)
    assert cancelled == 1  # 只有 queued 的 t2 被取消
    t2r = repos.task.find(t2)
    t1r = repos.task.find(t1)
    assert t2r is not None and t2r.status == "failed"
    assert t1r is not None and t1r.status == "running"


# ---------- 门禁域红线 ----------


def _published_with_design(repos: Repos, services: Services, tier: str = "B") -> str:
    req_id = make_req(repos, services, tier=tier)
    services.requirements.push_artifacts(req_id, {
        "digest": {"md": "# d", "risk_tier": tier, "fr_points": [{"id": "需求.FR-01", "name": "n"}]},
        "audit": {"md": "# a"},
        "items": [{"seq": "P01", "level": "red", "theme": "t", "question": "q"}]})
    services.requirements.set_answer(req_id, "P01", {"answer": "a"})
    services.requirements.publish(req_id, {"standardMd": "# s"})
    repos.fr_point.replace_all(req_id, [FrPoint(req_id=req_id, id="需求.FR-01", name="n")])
    repos.requirement.set_target_repos(req_id, '[{"role":"","kind":"local","ref":"/tmp/repo","branch":""}]')
    return req_id


def test_gate_open_requires_published_and_green(env: tuple[Repos, Services]) -> None:
    repos, services = env
    req_id = make_req(repos, services)  # 未发布
    with pytest.raises(ApiException) as e:
        services.gate.open(req_id, None)
    assert "发布后流程" in str(e.value)

    req_id = _published_with_design(repos, services)  # 已发布但设计未定稿、无用例
    with pytest.raises(ApiException) as e2:
        services.gate.open(req_id, None)
    assert "开闸三条件未满足" in str(e2.value)


def test_gate_release_and_case_mutual_recognition(env: tuple[Repos, Services]) -> None:
    """红线：GR-4 放行 = 全部裁决 ∧ 落改闭环 ∧ 机械校验全绿；放行视同用例关卡1 通过（互认）。"""
    repos, services = env
    req_id = _published_with_design(repos, services)
    # 备齐三条件：设计定稿 + 用例齐且全部定稿 + FR 全覆盖
    services.designs.finalize(req_id, DraftArtifact(req_id=req_id, type="design", md="§0 design"), "tester")
    services.cases.sync_upsert(None, _sync_body(req_id, [
        {"id": "TC-1", "title": "t", "points": ["需求.FR-01"], "status": "reviewed"}]))
    services.gate.open(req_id, {"by": "qa"})

    round_row = repos.gate.list_rounds(req_id)[0]
    repos.draft.upsert(DraftArtifact(
        req_id=req_id, type="gate_issues", md="issues: A-01 场景", task_id=1, updated_at=NOW), NOW)
    d = repos.draft.find(req_id, "gate_issues")
    assert d is not None
    d.content_json = '{"issues":[{"code":"A-01","sev":"mid","scene":"白话场景"}]}'
    repos.draft.upsert(d, NOW)
    rid = round_row.id
    assert rid is not None  # 开闸回填的轮 id（类型收窄）
    services.gate.ingest(req_id, rid, None)
    issue = repos.gate.list_issues(rid)[0]
    iid = issue.id
    assert iid is not None  # 入库回填（类型收窄；局部变量收窄不被调用边界打破）
    # 裁决落改：先缺落点应 400
    with pytest.raises(ApiException):
        services.gate.verdict(iid, {"verdict": "revise"})
    services.gate.verdict(iid, {"verdict": "revise", "revLocation": "a.py:L1"})
    # 落改未执行 → 放行拒绝
    with pytest.raises(ApiException) as e:
        services.gate.release(rid, {"by": "qa"})
    assert "落改未闭环" in str(e.value)
    services.gate.revision(iid, "a.py:L1", "done")
    out = services.gate.release(rid, {"by": "qa"})
    # 用例在放行前已全部定稿 → 互认转换 0 条（互认是防御性兜底：draft sdlc-test 用例自动置 reviewed）
    assert out["ok"] is True and out["casesConfirmed"] == 0
    assert repos.test_case.find(req_id, "TC-1").status == "reviewed"  # type: ignore[union-attr]


# ---------- 入参绑定防 500（批次2：裸 int()/len() 与同批重复 id） ----------


def test_sync_input_binding_no_500(env: tuple[Repos, Services]) -> None:
    """expectedVersion 非整数、items 非数组、同批重复 id → 400 且整批零写入（原为 500/IntegrityError）。"""
    repos, services = env
    req_id = make_req(repos, services)
    for bad in ("abc", 2.5, True, [1]):
        with pytest.raises(ApiException) as e:
            services.cases.sync_upsert(None, {"reqId": req_id, "items": [
                {"id": "TC-1", "title": "t", "expectedVersion": bad}]})
        assert e.value.status == 400 and "必须是整数" in str(e.value)
    with pytest.raises(ApiException) as e1:  # type: ignore[arg-type]
        services.cases.sync_upsert(None, {"reqId": req_id, "items": 123})
    assert e1.value.status == 400 and "必须是数组" in str(e1.value)
    with pytest.raises(ApiException) as e2:
        services.cases.sync_upsert(None, {"reqId": req_id, "items": [
            {"id": "TC-1", "title": "a"}, {"id": "TC-1", "title": "b"}]})
    assert e2.value.status == 400 and "用例编号重复：TC-1" in str(e2.value)
    assert repos.test_case.count_by_req(req_id) == 0  # 整批零写入


def test_sync_input_binding_artifacts_items(env: tuple[Repos, Services]) -> None:
    """POST /artifacts 的 items 非数组 → 400（原 len(int)/逐字符迭代）。"""
    repos, services = env
    req_id = make_req(repos, services)
    payload = {"digest": {"md": "# d"}, "audit": {"md": "# a"}, "items": 123}
    with pytest.raises(ApiException) as e:
        services.requirements.push_artifacts(req_id, payload)  # type: ignore[arg-type]
    assert e.value.status == 400 and "必须是数组" in str(e.value)


def test_form_update_expected_version_binding(env: tuple[Repos, Services]) -> None:
    """表单编辑 expectedVersion：垃圾值 400（原 500）；数字串宽松接受。"""
    repos, services = env
    req_id = make_req(repos, services)
    services.cases.form_create(req_id, {"title": "表单用例", "owner": "qa", "steps": "s", "expect": "e"})
    case_id = repos.test_case.list_by_req(req_id)[0].id
    with pytest.raises(ApiException) as e:
        services.cases.form_update(req_id, case_id, {"title": "改", "expectedVersion": "xyz"})
    assert e.value.status == 400 and "必须是整数" in str(e.value)
    out = services.cases.form_update(req_id, case_id, {"title": "改", "expectedVersion": "1"})
    assert out["title"] == "改"


def test_put_draft_task_id_validation(env: tuple[Repos, Services]) -> None:
    """taskId 非整数 → 400（原 TEXT 落库毒化草稿读取为持续 500）；合法值回读为 int。"""
    repos, services = env
    req_id = make_req(repos, services)
    with pytest.raises(ApiException) as e:
        services.tasks.put_draft(req_id, "standard", {"md": "# x" * 100, "taskId": "abc"})
    assert e.value.status == 400 and "必须是整数" in str(e.value)
    services.tasks.put_draft(req_id, "standard", {"md": "# y" * 100, "taskId": 5})
    d = services.tasks.get_draft(req_id, "standard")
    assert d.task_id == 5


def test_set_target_repos_type_guard(env: tuple[Repos, Services]) -> None:
    """repos 非数组 → 400（原 len(int) TypeError 500）。"""
    repos, services = env
    req_id = make_req(repos, services)
    with pytest.raises(ApiException) as e:
        services.designs.set_target_repos(req_id, {"repos": 123})
    assert e.value.status == 400 and "必须是数组" in str(e.value)


def test_jint_or_400_matrix() -> None:
    """宽松整型绑定穷举：缺失/None→default；int 原值；整值 float 取整；数字串转 int；bool/非整值/垃圾→400。"""
    from platform_sdlc.common import jint_or_400

    assert jint_or_400({}, "k", -1) == -1
    assert jint_or_400({"k": None}, "k", 7) == 7
    assert jint_or_400({"k": 5}, "k") == 5
    assert jint_or_400({"k": 5.0}, "k") == 5
    assert jint_or_400({"k": "42"}, "k") == 42
    assert jint_or_400({"k": " +7 "}, "k") == 7
    for bad in (True, 5.5, "abc", [1], {"x": 1}):
        with pytest.raises(ApiException) as e:
            jint_or_400({"k": bad}, "k")
        assert e.value.status == 400 and "必须是整数" in str(e.value)


# ---------- 事务与乐观锁缺口（批次3：检查移入 tx + version 谓词 + approve/publish 原子性） ----------


def test_sync_upsert_predicate_guards_concurrent_bump(env: tuple[Repos, Services]) -> None:
    """锁内检查后、UPDATE 前版本被并发推进 → 谓词拦下（0 行）转 409 conflicts，不盲写。

    用实例属性 hook 模拟 TOCTOU 窗口内的并发写（hook 与服务调用同线程，RLock 可重入不死锁）。"""
    repos, services = env
    req_id = make_req(repos, services)
    services.cases.sync_upsert(None, {"reqId": req_id, "items": [{"id": "TC-1", "title": "初版", "owner": "s"}]})
    assert repos.test_case.find(req_id, "TC-1").version == 1  # type: ignore[union-attr]

    orig = repos.test_case.update_from_sync

    def hooked(t: object, expected_version: int) -> int:
        repos.test_case.db.execute(
            "UPDATE test_case SET version=version+1, title='并发改' WHERE req_id=? AND id=?", (t.req_id, t.id))  # type: ignore[attr-defined]
        return orig(t, expected_version)  # type: ignore[arg-type]

    repos.test_case.update_from_sync = hooked  # type: ignore[method-assign]
    try:
        with pytest.raises(ConflictException) as e:
            services.cases.sync_upsert(None, {"reqId": req_id, "items": [{"id": "TC-1", "title": "盲写版"}]})
        assert e.value.conflicts[0]["id"] == "TC-1"
        assert e.value.conflicts[0]["currentVersion"] == 2  # hook 推进后的现值
    finally:
        del repos.test_case.update_from_sync  # 还原实例属性，回落类定义
    after = repos.test_case.find(req_id, "TC-1")
    # hook 推进发生在同一事务内，随 ROLLBACK 一并回滚（真实独立事务的推进不会被回滚，谓词已由上面 conflicts 断言证明）
    assert after is not None and after.version == 1 and after.title == "初版"  # 盲写未发生、批次零应用


def test_approve_atomic_and_exactly_once(env: tuple[Repos, Services]) -> None:
    """并发采纳同一草稿：恰 1 次成功（副作用只落一次），其余 404；失败路径草稿保留可重试。"""
    import concurrent.futures

    repos, services = env
    req_id = make_req(repos, services)
    services.tasks.put_draft(req_id, "digest_bundle", {
        "contentJson": '{"digest":{"md":"# d","risk_tier":"B","fr_points":[{"id":"需求.FR-01","name":"p"}]},'
                       '"audit":{"md":"# a","red":0,"yellow":0,"blue":0,"items":[]},'
                       '"items":[{"seq":"P01","level":"yellow","theme":"t","question":"q?","answer":"a"}]}'})
    def try_approve(_: int) -> dict[str, object]:
        try:
            return services.tasks.approve(req_id, "digest_bundle", "pm")  # type: ignore[return-value]
        except ApiException as e:  # 并发败者：草稿已被首个采纳删除 → 404 是预期终局
            assert e.status == 404
            return {}

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(try_approve, range(8)))
    ok = sum(1 for r in results if r.get("draftApproved"))
    assert ok == 1
    assert repos.clarify_item.counts(req_id)["total"] == 1  # push_artifacts 副作用只落一次
    assert repos.draft.find(req_id, "digest_bundle") is None

    # 失败路径回滚：库内 TC-1(v1)，cases 草稿带过期版本 99 → sync_upsert 冲突回滚，草稿保留
    services.cases.sync_upsert(None, {"reqId": req_id, "items": [{"id": "TC-1", "title": "存量", "owner": "s"}]})
    stale_cases = json.dumps({"reqId": req_id, "items": [{"id": "TC-1", "title": "x", "expectedVersion": 99}]})
    services.tasks.put_draft(req_id, "cases", {"contentJson": stale_cases})
    with pytest.raises(ConflictException):
        services.tasks.approve(req_id, "cases", "pm")
    assert repos.draft.find(req_id, "cases") is not None  # 采纳失败草稿未被删（可重试）


def test_publish_tx_all_or_nothing(env: tuple[Repos, Services]) -> None:
    """发布失败（存在未处置问题）→ 状态与草稿原样；成功 → published + standard 草稿清除。"""
    repos, services = env
    req_id = make_req(repos, services)
    services.tasks.put_draft(req_id, "standard", {"md": "# 草稿" * 60})
    with pytest.raises(ApiException) as e:
        services.requirements.publish(req_id, {"standardMd": "# 最终版"})
    assert e.value.status == 400 and "未处置" in str(e.value)
    assert repos.requirement.find(req_id).status == "clarifying"  # type: ignore[union-attr]
    assert repos.draft.find(req_id, "standard") is not None  # 失败不清草稿

    # 上面 400 源于 total=0（无澄清项也拒发布）——补一条再走
    services.requirements.push_artifacts(req_id, {
        "digest": {"md": "# d", "risk_tier": "B", "fr_points": []},
        "audit": {"md": "# a", "red": 0, "yellow": 0, "blue": 0, "items": []},
        "items": [{"seq": "P01", "level": "yellow", "theme": "t", "question": "q?", "answer": "a"}]})
    services.requirements.set_answer(req_id, "P01", {"answer": "已答"})
    out = services.requirements.publish(req_id, {"standardMd": "# 最终版"})
    assert out["ok"] is True
    assert repos.requirement.find(req_id).status == "published"  # type: ignore[union-attr]
    assert repos.draft.find(req_id, "standard") is None  # 发布终态清草稿


# ---------- 读面与口径（批次4：syncedAt null / 路径越界 / 发布豁免口径 / L2 存在性窗口） ----------


def test_list_synced_at_null_before_push(env: tuple[Repos, Services]) -> None:
    """未推送过的需求：列表 syncedAt 输出 null（原为字符串 "None"，前端真值误判已同步）。"""
    repos, services = env
    r = services.requirements.create({"name": "从未推送", "sourceType": "text", "sourceText": "#", "submittedBy": "t"})
    req_id = r.id
    row = next(x for x in repos.requirement.list_all(None, None) if x.id == req_id)
    assert row.synced_at is None  # 建卡落 SQL NULL（原 str(None) 出 "None" 字符串）
    services.requirements.push_artifacts(req_id, {
        "digest": {"md": "# d", "risk_tier": "B", "fr_points": []},
        "audit": {"md": "# a", "red": 0, "yellow": 0, "blue": 0, "items": []},
        "items": [{"seq": "P01", "level": "yellow", "theme": "t", "question": "q?", "answer": "a"}]})
    row2 = next(r for r in repos.requirement.list_all(None, None) if r.id == req_id)
    assert isinstance(row2.synced_at, str) and row2.synced_at  # 推送后为时间串


def test_workspace_escape_blocked(tmp_path: Path) -> None:
    """工作区越界：兄弟前缀目录（../ws-evil）必须被拦（原 startswith 前缀比较漏判）。"""
    from types import SimpleNamespace

    from platform_sdlc.agent.context import TaskRunContext
    from platform_sdlc.agent.tools import _resolve_in_workspace

    root = tmp_path / "ws"
    root.mkdir()
    (tmp_path / "ws-evil").mkdir()  # 共享前缀的兄弟目录
    (tmp_path / "ws-evil" / "f.txt").write_text("x")
    ctx = cast(TaskRunContext, SimpleNamespace(workspace=root))
    assert _resolve_in_workspace(ctx, "a/b.txt").is_relative_to(root.resolve())  # 正常相对路径
    for bad in ("../ws-evil/f.txt", "../../etc/passwd"):
        with pytest.raises(ValueError, match="路径越界"):
            _resolve_in_workspace(ctx, bad)


def test_safe_static_file_sibling_prefix(tmp_path: Path) -> None:
    """静态兜底路由：兄弟前缀目录文件不得命中（原 startswith 漏判）。"""
    from platform_sdlc.main import _safe_static_file

    root = tmp_path / "static"
    (root / "assets").mkdir(parents=True)
    (root / "favicon.svg").write_text("<svg/>")
    (tmp_path / "static-backup").mkdir()
    (tmp_path / "static-backup" / "secret.db").write_text("s")
    assert _safe_static_file(root, "favicon.svg") is not None
    assert _safe_static_file(root, "../static-backup/secret.db") is None
    assert _safe_static_file(root, "spa/route") is None  # 无扩展名走 SPA 回退


def test_settle_terminal_exempts_post_publish_types(env: tuple[Repos, Services]) -> None:
    """发布收尾豁免面与 D36 统一：质检/标准文档取消，case/design/gate 排队任务存活。"""
    repos, services = env
    req_id = make_req(repos, services, status="clarifying")
    services.requirements.push_artifacts(req_id, {
        "digest": {"md": "# d", "risk_tier": "B", "fr_points": []},
        "audit": {"md": "# a", "red": 0, "yellow": 0, "blue": 0, "items": []},
        "items": [{"seq": "P01", "level": "yellow", "theme": "t", "question": "q?", "answer": "a"}]})
    services.requirements.set_answer(req_id, "P01", {"answer": "已答"})
    services.tasks.create(req_id, {"type": "quality_check"})
    services.tasks.create(req_id, {"type": "standard_doc"})
    services.requirements.publish(req_id, {"standardMd": "# 终版"})
    services.orchestration.on_published(req_id)  # 发布入口后的 L2 编排（settle + 自动排 case_gen）
    services.tasks.create(req_id, {"type": "gate_review"})  # 手动建门禁（design_gen 需配仓不在本例）
    statuses = {t.type: t.status for t in repos.task.list(req_id, None)}
    assert statuses["quality_check"] == "failed" and statuses["standard_doc"] == "failed"
    assert statuses["case_gen"] == "queued" and statuses["gate_review"] == "queued"  # 豁免面内存活


def test_gate_review_requires_published(env: tuple[Repos, Services]) -> None:
    """门禁属发布后流程：未发布建 gate_review → 409（与 gate.open 同口径，堵排队即作废的口径缝隙）。"""
    repos, services = env
    req_id = make_req(repos, services, status="clarifying")
    with pytest.raises(ApiException) as e:
        services.tasks.create(req_id, {"type": "gate_review"})
    assert e.value.status == 409 and "门禁属发布后流程" in str(e.value)


def test_any_task_of_type_beyond_limit_100(env: tuple[Repos, Services]) -> None:
    """L2「只自动一次」不受 LIMIT 100 窗口影响（原 list() 溢出后误判从未建过 → 已取消任务复活）。"""
    from platform_sdlc.models import Task

    repos, services = env
    req_id = make_req(repos, services)
    repos.task.insert(Task(id=0, req_id=req_id, type="standard_doc", status="failed",
                           payload="", result="", runner="", created_by="auto",
                           created_at=NOW))  # 最老一行，先于后续 101 条
    for _ in range(101):
        repos.task.insert(Task(id=0, req_id=req_id, type="quality_check", status="done",
                               payload="", result="", runner="", created_by="",
                               created_at=NOW))
    assert services.tasks.any_task_of_type(req_id, "standard_doc") is True


def test_media_upload_same_id_concurrent_idempotent(env: tuple[Repos, Services]) -> None:
    """并发上传同 mediaId：find→insert 已入 tx，后进者幂等返回（原撞唯一键 IntegrityError 500）。"""
    import concurrent.futures

    repos, services = env
    req_id = make_req(repos, services)
    payload = b"x" * 64
    def _upload(_: int) -> MediaFile:
        return services.media.upload(req_id, "a.png", "image/png", payload, explicit_id="m-000000000001")

    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        outs = list(pool.map(_upload, range(6)))
    assert all(f.media_id == "m-000000000001" for f in outs)
    assert len(repos.media.list_by_req(req_id)) == 1  # 只落一行
