"""P3 冒烟：HTTP 层键形契约（camelCase、错误体形状、端点行为）端到端验证。"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from platform_sdlc.config import Settings
from platform_sdlc.main import create_app


@pytest.fixture()
def client(tmp_path: Path) -> Iterator[TestClient]:
    # _env_file 为 pydantic-settings 运行时私参（类型 stub 未声明），传 None 隔离本机 .env
    settings = Settings(
        # _env_file 为 pydantic-settings 运行时私参（类型 stub 未声明），传 None 隔离本机 .env
        _env_file=None,  # pyright: ignore[reportCallIssue]
        db_path=str(tmp_path / "data" / "platform.db"), media_dir=str(tmp_path / "data" / "media"))
    with TestClient(create_app(settings, cwd=tmp_path)) as c:
        yield c


def _create(client: TestClient, **overrides: object) -> dict[str, object]:
    body: dict[str, object] = {"name": "冒烟需求", "sourceType": "text", "sourceText": "# 原文", **overrides}
    r = client.post("/api/requirements", json=body)
    assert r.status_code == 200, r.text
    return r.json()  # type: ignore[no-any-return]


def test_requirement_flow_shapes(client: TestClient) -> None:
    created = _create(client, iter="2026-W40")
    assert created == {"id": "REQ-2026-001", "status": "submitted"}

    listed = client.get("/api/requirements", params={"iter": "2026-W40"}).json()
    assert set(listed[0]) == {"id", "name", "iter", "sourceType", "status", "submittedBy",
                              "createdAt", "syncedAt", "riskTier", "totalQ", "answeredQ", "caseCount"}

    push = client.post("/api/requirements/REQ-2026-001/artifacts", json={
        "digest": {"md": "# 梳理", "risk_tier": "B", "fr_points": [{"id": "需求.FR-01", "name": "登录"}]},
        "audit": {"md": "# 体检", "red": 1, "yellow": 2, "blue": 0, "items": [
            {"id": "体检.P01", "level": "red", "title": "t", "where": "w", "go": "g", "group": "登录"}]},
        "items": [{"seq": "P01", "level": "red", "theme": "登录", "question": "密码规则？"}],
        "snapshot": {"md": "# 原文全文"},
    })
    assert push.status_code == 200
    assert set(push.json()) == {"ok", "items", "frPoints", "carriedAnswers", "changedQuestions",
                                "snapshotUpdated", "docUpdated", "missingMedia", "danglingPids"}

    detail = client.get("/api/requirements/REQ-2026-001").json()
    assert set(detail) >= {"card", "items", "digest", "audit", "snapshot", "domains",
                           "frCount", "frPoints", "coverage", "quality", "answered", "totalQ"}
    assert "doc" not in detail  # NON_NULL：无 doc 块时键缺失（verify-m4 降级契约）
    assert set(detail["card"]) == {"id", "name", "iter", "sourceType", "sourceUrl", "sourceText",
                                   "status", "submittedBy", "standardDoc", "createdAt", "syncedAt",
                                   "riskTier", "contentRev"}  # contentRev=v4.12 增键
    assert detail["quality"] == {"stars": 3, "basis": "阻断 1 项"}

    ans = client.put("/api/requirements/REQ-2026-001/answers/P01", json={"answer": "8 位"})
    assert ans.json() == {"ok": True}
    # L2：全部答完自动排 standard_doc（v4-4：建卡自动排的 source_import 同在列表——HTTP 层已触发）
    tasks = client.get("/api/tasks", params={"req_id": "REQ-2026-001"}).json()
    assert {t["type"]: t["createdBy"] for t in tasks}["standard_doc"] == "auto:clarify-complete"
    assert any(t["type"] == "source_import" and t["createdBy"] == "auto:card-created" for t in tasks)

    published = client.post("/api/requirements/REQ-2026-001/publish", json={"standardMd": "# 标准文档"})
    assert published.json() == {"ok": True, "publishedAt": published.json()["publishedAt"]}
    # 草稿与质检任务随发布收尾作废，case_gen 自动排队（D36 发布后合法）
    tasks2 = client.get("/api/tasks", params={"req_id": "REQ-2026-001"}).json()
    assert any(t["type"] == "case_gen" and t["createdBy"] == "auto:published" for t in tasks2)


def test_error_shapes(client: TestClient) -> None:
    assert client.get("/api/requirements/REQ-9999-001").status_code == 404
    r = client.post("/api/requirements", json={"name": "", "sourceType": "text"})
    assert r.status_code == 400 and r.json() == {"error": "缺少必填字段：name"}
    r2 = client.post("/api/requirements", content=b"not-json", headers={"Content-Type": "application/json"})
    assert r2.status_code == 400 and r2.json() == {"error": "请求体不是合法 JSON 或字段类型不符"}
    r3 = client.post("/api/requirements", content=b"x=1", headers={"Content-Type": "application/x-www-form-urlencoded"})
    assert r3.status_code == 415 and r3.json()["error"].startswith("请求体类型不支持：")


def test_case_and_task_and_agent_shapes(client: TestClient) -> None:
    _create(client)
    sync = client.post("/api/cases", json={"reqId": "REQ-2026-001", "items": [
        {"id": "TC-1", "title": "用例", "points": ["需求.FR-01"], "verifyRefs": ["体检.P01"]}]})
    assert set(sync.json()) == {"ok", "inserted", "updated", "removed", "removedIds"}

    # v4-4：先消化建卡自动排的 source_import（领走即 done——本夹具不测导入，块表由推 doc 补位）
    auto_import = client.post("/api/tasks/next", params={"runner": "smoke"}).json()
    assert auto_import["type"] == "source_import"
    client.post(f"/api/tasks/{auto_import['id']}/failed", json={"error": "api-shape fixture 跳过导入"})
    # v4-4：quality_check 领取有块表非空守卫（本夹具跳过导入）——形状/状态机断言改用 case_gen
    task = client.post("/api/requirements/REQ-2026-001/tasks", json={"type": "case_gen"}).json()
    assert set(task) == {"id", "reqId", "type", "status", "payload", "result", "runner", "attempt",
                         "createdBy", "createdAt", "updatedAt"}
    claimed = client.post("/api/tasks/next", params={"runner": "smoke"})
    assert claimed.status_code == 200 and claimed.json()["status"] == "running"
    assert client.post("/api/tasks/next").status_code == 204  # 队列空
    done = client.post(f"/api/tasks/{task['id']}/done", json={"result": '{"k":1}'})
    assert done.json()["status"] == "done"

    put = client.put("/api/requirements/REQ-2026-001/drafts/cases",
                     json={"contentJson": '{"items":[{"id":"TC-9","title":"草稿用例"}]}'})
    assert put.json()["type"] == "cases"
    approve = client.post("/api/requirements/REQ-2026-001/drafts/cases/approve", json={})
    assert approve.json()["draftApproved"] is True and approve.json()["inserted"] == 1

    st = client.get("/api/agent/status").json()
    assert set(st) == {"enabled", "paused", "running"}
    assert client.post("/api/agent/pause").json() == st  # P3 无 agent，幂等可用


def test_iteration_and_design_and_gate_shapes(client: TestClient) -> None:
    assert client.post("/api/iterations", json={"name": "2026-W41"}).json() == {"name": "2026-W41"}
    iters = client.get("/api/iterations").json()
    assert set(iters[0]) == {"name", "note", "createdBy", "createdAt", "reqId", "reqName", "reqStatus",
                             "riskTier", "submittedBy", "totalQ", "answeredQ", "caseCount",
                             "designPhase", "gateStage"}
    _create(client, iter="2026-W41")
    detail = client.get("/api/iterations/detail", params={"name": "2026-W41"}).json()
    assert detail["designPhase"] == "none" and detail["gateStage"] == "none"

    _create(client)
    assert client.put("/api/requirements/REQ-2026-001/target-repos",
                      json={"repos": [{"kind": "local", "ref": "/tmp/r"}]}).json() == {"ok": True}
    dv = client.get("/api/requirements/REQ-2026-001/design").json()
    assert dv["phase"] == "none" and dv["repos"][0]["kind"] == "local"

    gv = client.get("/api/requirements/REQ-2026-001/gate").json()
    assert gv["phase"] == "blocked" and len(gv["checks"]) == 3
    assert client.get("/api/requirements/REQ-2026-001/gate/change-status").json()["changed"] is False


def test_draft_task_id_binding_400(client: TestClient) -> None:
    """PUT /drafts 的 taskId 非整数 → 400 {error}（原 500 / 落库毒化）。"""
    _create(client)
    r = client.put("/api/requirements/REQ-2026-001/drafts/standard",
                   json={"md": "# x", "taskId": "abc"})
    assert r.status_code == 400 and "必须是整数" in r.json()["error"]
