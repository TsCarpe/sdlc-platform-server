"""v4.11 验收：变更单数据面——draft CRUD（items 全量重算 / blk- 草稿期平台 ID 不漂移）、
影响面预览（edit/move 只标直接锚定、delete 子孙递归 CTE、heading 删不带章节体、预览不冻结）。

契约（DESIGN-v4 §5.2/§5.3/§5.4）：draft 不碰块表（随时可提不限状态）；diff 复用 v4-7 的
BlockService.diff_blocks（LIS 判 move）；impact 冻结章节一律 heading block_id 禁 seq 数值。
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from platform_sdlc.config import Settings
from platform_sdlc.db import Database
from platform_sdlc.main import create_app
from platform_sdlc.models import ReqBlock
from platform_sdlc.repositories import Repos

_BODY_TITLE = {"title": "口径修订", "detail": "PM 确认后提单", "proposedBy": "张三"}


@pytest.fixture()
def env(tmp_path: Path) -> Iterator[tuple[TestClient, Database, Repos]]:
    settings = Settings(_env_file=None, db_path=str(tmp_path / "data" / "platform.db"),  # pyright: ignore[reportCallIssue]
                        media_dir=str(tmp_path / "data" / "media"))
    app = create_app(settings, cwd=tmp_path)
    client = TestClient(app, raise_server_exceptions=False)
    db = Database(tmp_path / "data" / "platform.db")
    repos = Repos.create(db)
    yield client, db, repos


def _seed(repos: Repos, req_id: str) -> None:
    """5 块基线 + 锚定面：P1/P2 澄清项、FR-1、digest feature 行、关联用例。"""
    repos.req_block.replace_all(req_id, [
        ReqBlock(req_id=req_id, block_id="t1", seq=1000, type="title", text="标题"),
        ReqBlock(req_id=req_id, block_id="h1", seq=2000, type="heading", text="背景",
                 attrs_json='{"level":2}'),
        ReqBlock(req_id=req_id, block_id="p1", seq=3000, type="paragraph", text="一段说明"),
        ReqBlock(req_id=req_id, block_id="tr1", seq=4000, type="table-row", text="",
                 attrs_json='{"head":["列A"],"cells":[["v1"]]}'),
        ReqBlock(req_id=req_id, block_id="trc1", seq=5000, type="paragraph", text="行内块",
                 attrs_json='{"parent":"tr1"}'),
        ReqBlock(req_id=req_id, block_id="li1", seq=6000, type="list-item", text="列表项一",
                 attrs_json='{"marker":"1. ","depth":1,"parent":null}'),
        ReqBlock(req_id=req_id, block_id="li2", seq=7000, type="list-item", text="子项 a",
                 attrs_json='{"marker":"a. ","depth":2,"parent":"li1"}'),
    ])
    repos.clarify_item.insert(_ci(req_id, "P1", "p1", "q1"))
    repos.clarify_item.insert(_ci(req_id, "P2", "li2", "q2"))
    repos.clarify_item.insert(_ci(req_id, "P3", "", "q3（空 anchor 永不进影响面）"))
    repos.clarify_item.insert(_ci(req_id, "P4", "trc1", "q4（行内子块锚定）"))
    repos.fr_point.replace_all(req_id, [
        _fp(req_id, "需求.FR-1", "注册", "p1"), _fp(req_id, "需求.FR-2", "计费", "li2")])
    repos.artifact.upsert(req_id, "digest", "", _digest_json(), "2026-10-13 00:00:00")
    repos.test_case.insert(_case(req_id, "TC-1", ["需求.FR-1", "需求.FR-2"]))


def _ci(req_id: str, seq: str, anchor: str, question: str) -> Any:
    from platform_sdlc.models import ClarifyItem
    return ClarifyItem(id=0, req_id=req_id, seq=seq, level="yellow", theme="t",
                       question=question, anchor=anchor, status="pending", origin="skill")


def _fp(req_id: str, pid: str, name: str, anchor: str) -> Any:
    from platform_sdlc.models import FrPoint
    return FrPoint(req_id=req_id, id=pid, name=name, source="", group="", module="", anchor=anchor)


def _digest_json() -> str:
    import json
    return json.dumps({"summary": "s", "roles": [], "features": [
        {"role": "用户", "m": "注册", "func": "手机号注册", "cov": "", "src": "", "anchor": "p1"},
        {"role": "用户", "m": "计费", "func": "按量计费", "cov": "", "src": "", "anchor": "li2"}],
        "flows": [], "states": []}, ensure_ascii=False)


def _case(req_id: str, cid: str, points: list[str]) -> Any:
    import json

    from platform_sdlc.models import TestCase
    return TestCase(req_id=req_id, id=cid, title="关联用例", points=json.dumps(points), priority="P1",
                    pre="", steps="s", expect="e", owner="qa", tool="sdlc-test", status="reviewed")


# ---------- draft CRUD ----------

def test_draft_create_and_items(env: tuple[TestClient, Database, Repos]) -> None:
    client, db, repos = env
    req_id = _mk_card(client)
    _seed(repos, req_id)

    # proposedBy 必填（blocks 先过块校验，proposedBy 缺失才轮到它报）
    bad = client.post(f"/api/requirements/{req_id}/changes",
                      json={**{k: v for k, v in _BODY_TITLE.items() if k != "proposedBy"},
                            "blocks": [{"id": "t1", "type": "title", "text": "标题"}]})
    assert bad.status_code == 400 and "proposedBy" in bad.json()["error"]
    db.execute("UPDATE requirement SET status='published' WHERE id=?", (req_id,))
    ok = client.post(f"/api/requirements/{req_id}/changes", json={**_BODY_TITLE, "blocks": [
        {"id": "t1", "type": "title", "text": "标题"},
        {"id": "h1", "type": "heading", "level": 2, "text": "背景"},
        {"id": "p1", "type": "paragraph", "text": "一段说明（修订）"},           # edit
        {"type": "paragraph", "text": "补充说明"},                                # insert（草稿期即有平台 ID）
        {"id": "li2", "type": "list-item", "marker": "a. ", "depth": 1, "text": "子项 a"},  # li1 删→li2 上提=edit
    ]})  # li1/tr1/trc1 不提交 = delete（tr1 连行内子块 trc1 一起删——§2.3 约束）
    assert ok.status_code == 200
    out = ok.json()
    assert out["status"] == "draft" and out["proposedBy"] == "张三"
    assert out["counts"] == {"edit": 2, "insert": 1, "delete": 3, "move": 0}  # li1+tr1+trc1
    edits = {i["blockId"]: i for i in out["items"] if i["op"] == "edit"}
    assert edits["p1"]["oldText"] == "一段说明" and edits["p1"]["newText"] == "一段说明（修订）"
    assert edits["li2"]["newAttrs"] == {"marker": "a. ", "depth": 1}  # 上提：parent 剥 None 后消失
    assert {i["blockId"] for i in out["items"] if i["op"] == "delete"} == {"li1", "tr1", "trc1"}
    insert_id = next(i["blockId"] for i in out["items"] if i["op"] == "insert")
    assert insert_id.startswith("blk-") and len(insert_id) == 30  # §5.2 草稿期即有平台 ID
    # 删列表父项留子块（未上提）→ parent 拓扑校验拒（列表子项删父须上提，对齐飞书）
    bad_topo = client.post(f"/api/requirements/{req_id}/changes", json={**_BODY_TITLE, "blocks": [
        {"id": "t1", "type": "title", "text": "标题"},
        {"id": "li2", "type": "list-item", "marker": "a. ", "depth": 2, "parent": "li1", "text": "子项 a"},
    ]})
    assert bad_topo.status_code == 400 and "未在本批之前出现" in bad_topo.json()["error"]
    # draft 不碰块表（§5.3）：块表现值原样
    assert [b.block_id for b in repos.req_block.list_by_req(req_id)] ==         ["t1", "h1", "p1", "tr1", "trc1", "li1", "li2"]


def test_draft_save_recalc_and_id_stable(env: tuple[TestClient, Database, Repos]) -> None:
    client, db, repos = env
    req_id = _mk_card(client)
    _seed(repos, req_id)
    created = client.post(f"/api/requirements/{req_id}/changes", json={**_BODY_TITLE, "blocks": [
        {"id": "t1", "type": "title", "text": "标题"},
        {"id": "h1", "type": "heading", "level": 2, "text": "背景"},
        {"id": "p1", "type": "paragraph", "text": "一段说明（修订）"},
        {"type": "paragraph", "text": "补充说明"},
        {"id": "tr1", "type": "table-row", "head": ["列A"], "cells": [["v1"]]},
        {"id": "trc1", "type": "paragraph", "parent": "tr1", "text": "行内块"},
        {"id": "li1", "type": "list-item", "marker": "1. ", "depth": 1, "text": "列表项一"},
        {"id": "li2", "type": "list-item", "marker": "a. ", "depth": 2, "parent": "li1", "text": "子项 a"},
    ]}).json()
    cid = created["id"]
    first_insert = next(i["blockId"] for i in created["items"] if i["op"] == "insert")
    n_items = db.query_scalar("SELECT COUNT(*) FROM req_change_item WHERE change_id=?", (cid,))

    # 反复保存：items 全量重算替换（不叠加）；blk- 沿用（前端带平台 ID 回提不漂移）
    saved = client.put(f"/api/requirements/{req_id}/changes/{cid}", json={
        "title": "口径修订 v2",
        "blocks": [
            {"id": "t1", "type": "title", "text": "标题"},
            {"id": "h1", "type": "heading", "level": 2, "text": "背景（改）"},   # 新增第二个 edit
            {"id": "p1", "type": "paragraph", "text": "一段说明（修订）"},
            {"id": first_insert, "type": "paragraph", "text": "补充说明"},      # 沿用平台 ID
            {"id": "tr1", "type": "table-row", "head": ["列A"], "cells": [["v1"]]},
            {"id": "trc1", "type": "paragraph", "parent": "tr1", "text": "行内块"},
            {"id": "li1", "type": "list-item", "marker": "1. ", "depth": 1, "text": "列表项一"},
            {"id": "li2", "type": "list-item", "marker": "a. ", "depth": 2, "parent": "li1", "text": "子项 a"},
        ]}).json()
    assert saved["title"] == "口径修订 v2"
    assert saved["counts"] == {"edit": 2, "insert": 1, "delete": 0, "move": 0}
    assert db.query_scalar("SELECT COUNT(*) FROM req_change_item WHERE change_id=?", (cid,)) == n_items + 1
    # blk- 沿用：insert 项平台 ID 不漂移（反复保存前端引用稳定，§5.2）
    assert next(i["blockId"] for i in saved["items"] if i["op"] == "insert") == first_insert

    # draft 删除（items 级联）；confirmed 不可删不可存
    assert client.delete(f"/api/requirements/{req_id}/changes/{cid}").json() == {"ok": True}
    assert db.query_scalar("SELECT COUNT(*) FROM req_change_item WHERE change_id=?", (cid,)) == 0
    assert client.get(f"/api/requirements/{req_id}/changes/{cid}").status_code == 404

    c2 = client.post(f"/api/requirements/{req_id}/changes", json={**_BODY_TITLE, "blocks": [
        {"id": "t1", "type": "title", "text": "标题"},
        {"id": "h1", "type": "heading", "level": 2, "text": "背景"},
        {"id": "p1", "type": "paragraph", "text": "一段说明"},
        {"id": "tr1", "type": "table-row", "head": ["列A"], "cells": [["v1"]]},
        {"id": "trc1", "type": "paragraph", "parent": "tr1", "text": "行内块"},
        {"id": "li1", "type": "list-item", "marker": "1. ", "depth": 1, "text": "列表项一"},
        {"id": "li2", "type": "list-item", "marker": "a. ", "depth": 2, "parent": "li1", "text": "子项 a"},
    ]}).json()
    db.execute("UPDATE req_change SET status='confirmed' WHERE id=?", (c2["id"],))
    r1 = client.put(f"/api/requirements/{req_id}/changes/{c2['id']}", json={**_BODY_TITLE, "blocks": []})
    assert r1.status_code == 409 and "冻结快照" in r1.json()["error"]
    r2 = client.delete(f"/api/requirements/{req_id}/changes/{c2['id']}")
    assert r2.status_code == 409 and "confirmed 不作废" in r2.json()["error"]


# ---------- 影响面预览 ----------

def test_preview_impact_mechanics(env: tuple[TestClient, Database, Repos]) -> None:
    client, db, repos = env
    req_id = _mk_card(client)
    _seed(repos, req_id)
    created = client.post(f"/api/requirements/{req_id}/changes", json={**_BODY_TITLE, "blocks": [
        {"id": "t1", "type": "title", "text": "标题"},
        {"id": "h1", "type": "heading", "level": 2, "text": "背景"},
        {"id": "p1", "type": "paragraph", "text": "一段说明（修订）"},   # edit：直接锚定
        {"type": "paragraph", "text": "补充说明"},                       # insert：不进影响面
        {"id": "li1", "type": "list-item", "marker": "1. ", "depth": 1, "text": "列表项一"},
        {"id": "li2", "type": "list-item", "marker": "a. ", "depth": 2, "parent": "li1", "text": "子项 a"},
    ]}).json()  # tr1 delete：行内子块 trc1 连删（§2.3 约束）→ 子孙经 CTE 进全集
    impact = client.get(f"/api/requirements/{req_id}/changes/{created['id']}/impact").json()

    # 块集 = edit(p1) ∪ delete 子孙 CTE(tr1→trc1)；insert 新块与未动块（li1/li2）不进
    assert impact["blocks"] == ["p1", "tr1", "trc1"]
    # 四类清单：P1(p1)/P4(trc1) 命中、P2(li2 未动)/P3(空 anchor) 不进；FR-1 命中；用例经 FR 关联；digest 行命中
    assert [c["seq"] for c in impact["clarifyItems"]] == ["P1", "P4"]
    assert [f["id"] for f in impact["frPoints"]] == ["需求.FR-1"]
    assert [(c["id"], c["frPoints"]) for c in impact["cases"]] == [("TC-1", ["需求.FR-1"])]
    assert {(d["kind"], d["anchor"]) for d in impact["digestRows"]} == {("feature", "p1")}
    # 章节 = heading block_id（禁 seq 数值：输出结构无 seq）
    assert impact["sections"] == [{"headingId": "h1", "path": "背景"}]
    assert "seq" not in str(impact["sections"])
    # 预览不冻结（§5.4：impact_json 仅 confirm 时写——v4-12）
    row = db.query_one("SELECT impact_json FROM req_change WHERE id=?", (created["id"],))
    assert row is not None and row["impact_json"] == ""


def test_preview_heading_delete_no_section_body(env: tuple[TestClient, Database, Repos]) -> None:
    """heading 删不带章节体（§5.4 对齐飞书：删标题块只删标题行，章节体上提）——
    章节内其他块的锚定行不进影响面。"""
    client, _, repos = env
    req_id = _mk_card(client)
    _seed(repos, req_id)
    created = client.post(f"/api/requirements/{req_id}/changes", json={**_BODY_TITLE, "blocks": [
        {"id": "t1", "type": "title", "text": "标题"},
        {"id": "p1", "type": "paragraph", "text": "一段说明"},
        {"id": "tr1", "type": "table-row", "head": ["列A"], "cells": [["v1"]]},
        {"id": "trc1", "type": "paragraph", "parent": "tr1", "text": "行内块"},
        {"id": "li1", "type": "list-item", "marker": "1. ", "depth": 1, "text": "列表项一"},
        {"id": "li2", "type": "list-item", "marker": "a. ", "depth": 2, "parent": "li1", "text": "子项 a"},
    ]}).json()  # h1 delete
    impact = client.get(f"/api/requirements/{req_id}/changes/{created['id']}/impact").json()
    assert impact["blocks"] == ["h1"]  # 无子块、章节体不扩散
    assert impact["clarifyItems"] == []  # P1(p1)/P2(li2)/P4(trc1) 均不进（章节体锚定不进）
    # 被删 heading 自身所在章节进 sections（章节消失是影响后果）；判据=章节体锚定行不进（上行）
    assert impact["sections"] == [{"headingId": "h1", "path": "背景"}]


def test_change_guards(env: tuple[TestClient, Database, Repos]) -> None:
    client, _, _ = env
    req_id = _mk_card(client)
    # 块表空（无 diff 基线）→ 409
    r = client.post(f"/api/requirements/{req_id}/changes",
                    json={**_BODY_TITLE, "blocks": [{"type": "paragraph", "text": "x"}]})
    assert r.status_code == 409 and "先完成 source_import" in r.json()["error"]
    assert client.post("/api/requirements/REQ-NOPE/changes",
                       json={**_BODY_TITLE, "blocks": []}).status_code == 404
    assert client.get("/api/requirements/REQ-NOPE/changes").status_code == 404


def _mk_card(client: TestClient, name: str = "变更单验证") -> str:
    created = client.post("/api/requirements", json={"name": name, "sourceType": "text",
                                                     "sourceText": "# 原文", "submittedBy": "tester"}).json()
    return str(created["id"])


# ---------- confirm 事务（§5.5；v4-12） ----------

def _publish_card(client: TestClient, repos: Repos, req_id: str) -> None:
    """发布链：推产物（P1-P4 重建）→ 全处置 → publish。种子块/锚定不动。"""
    client.post(f"/api/requirements/{req_id}/artifacts", json={
        "digest": {"md": "", "summary": "s", "roles": [], "flows": [], "states": [], "features": [
            {"role": "用户", "m": "注册", "func": "手机号注册", "anchor": "p1"}]},
        "audit": {"md": "", "red": 0, "yellow": 0, "blue": 0, "items": []},
        "items": [{"seq": "P1", "level": "yellow", "theme": "t", "question": "q1", "anchor": "p1"},
                  {"seq": "P2", "level": "yellow", "theme": "t", "question": "q2", "anchor": "li2"},
                  {"seq": "P3", "level": "yellow", "theme": "t", "question": "q3"},
                  {"seq": "P4", "level": "yellow", "theme": "t", "question": "q4", "anchor": "trc1"}],
    })
    for seq in ("P1", "P2", "P3", "P4"):
        client.put(f"/api/requirements/{req_id}/answers/{seq}", json={"answer": f"答 {seq}"})
    client.post(f"/api/requirements/{req_id}/publish", json={"standardMd": "# 定稿"})


def _draft_edit_p1(client: TestClient, req_id: str, title: str = "口径修订") -> Any:
    return client.post(f"/api/requirements/{req_id}/changes", json={
        "title": title, "proposedBy": "张三", "blocks": [
            {"id": "t1", "type": "title", "text": "标题"},
            {"id": "h1", "type": "heading", "level": 2, "text": "背景"},
            {"id": "p1", "type": "paragraph", "text": "一段说明（修订）"},
            {"type": "paragraph", "text": "变更新增段"},
            {"id": "li1", "type": "list-item", "marker": "1. ", "depth": 1, "text": "列表项一"},
            {"id": "li2", "type": "list-item", "marker": "a. ", "depth": 2, "parent": "li1", "text": "子项 a"},
        ]}).json()  # tr1/trc1 delete


def test_confirm_transaction(env: tuple[TestClient, Database, Repos]) -> None:
    client, db, repos = env
    req_id = _mk_card(client)
    _seed(repos, req_id)
    _publish_card(client, repos, req_id)
    cid = _draft_edit_p1(client, req_id)["id"]

    out = client.post(f"/api/requirements/{req_id}/changes/{cid}/confirm",
                      json={"confirmedBy": "李四"}).json()
    # changed（表外通道——通用 set_status 不认此流转）+ content_rev+1 + impact 冻结
    assert out["status"] == "confirmed" and out["contentRev"] == 1 and out["impact"]["counts"]["blocks"] >= 1
    card = client.get(f"/api/requirements/{req_id}").json()["card"]
    assert card["status"] == "changed" and card["contentRev"] == 1
    assert client.post(f"/api/requirements/{req_id}/status", json={"status": "changed"}).status_code == 409

    # items 应用：edit 文本/version+1/留痕；insert 落 blk-；delete 软删；seq 重排按留底数组
    rows = {r["block_id"]: r for r in db.query_all("SELECT * FROM req_block WHERE req_id=?", (req_id,))}
    assert rows["p1"]["text"] == "一段说明（修订）" and rows["p1"]["version"] == 2
    assert rows["p1"]["updated_by"] == "李四"
    new_blocks = [b for b in repos.req_block.list_by_req(req_id) if b.block_id.startswith("blk-")]
    assert [b.text for b in new_blocks] == ["变更新增段"] and new_blocks[0].version == 1
    assert rows["tr1"]["deleted"] != "" and rows["trc1"]["deleted"] != ""
    nid = new_blocks[0].block_id
    assert [b.block_id for b in repos.req_block.list_by_req(req_id)] == ["t1", "h1", "p1", nid, "li1", "li2"]

    # 澄清重开（冻结影响面锚 p1/trc1）：P1/P4 回 pending + note 前缀；P2(li2 未动)/P3(空) 不动
    answers = {a["seq"]: a for a in client.get(f"/api/requirements/{req_id}/answers").json()}
    assert answers["P1"]["status"] == "pending" and answers["P1"]["answer"] == ""
    assert "因变更单#1 重开，原答复：答 P1" in answers["P1"]["note"]
    assert answers["P4"]["status"] == "pending"
    assert answers["P2"]["answer"] == "答 P2" and answers["P3"]["answer"] == "答 P3"

    # 重复 confirm 409
    again = client.post(f"/api/requirements/{req_id}/changes/{cid}/confirm", json={"confirmedBy": "x"})
    assert again.status_code == 409 and "不可确认" in again.json()["error"]


def test_confirm_optimistic_lock_and_atomicity(env: tuple[TestClient, Database, Repos]) -> None:
    """draft 后块表被窗口 1 编辑 → old_text 不符整单 409，零写入（原子性）。"""
    client, db, repos = env
    req_id = _mk_card(client)
    _seed(repos, req_id)
    _publish_card(client, repos, req_id)
    cid = _draft_edit_p1(client, req_id)["id"]

    # 窗口 1 编辑同块（draft 与编辑并存期间漂移——published 拒编辑？changed 前的 published 卡不可编辑，
    # 用 db 直改模拟「后续修改」）
    db.execute("UPDATE req_block SET text='被并发修改的文本' WHERE req_id=? AND block_id='p1'", (req_id,))
    r = client.post(f"/api/requirements/{req_id}/changes/{cid}/confirm", json={"confirmedBy": "李四"})
    assert r.status_code == 409 and "已被后续修改" in r.json()["error"]
    # 原子性：状态/rev/块表/items 全无变化
    card = client.get(f"/api/requirements/{req_id}").json()["card"]
    assert card["status"] == "published" and card["contentRev"] == 0
    row = db.query_one("SELECT text FROM req_block WHERE req_id=? AND block_id='p1'", (req_id,))
    assert row is not None and row["text"] == "被并发修改的文本"
    assert db.query_scalar("SELECT status FROM req_change WHERE id=?", (cid,)) == "draft"


def test_confirm_guards_serial(env: tuple[TestClient, Database, Repos]) -> None:
    client, db, repos = env
    req_id = _mk_card(client)
    _seed(repos, req_id)
    # 未发布拒（窗口 1 直接编辑是未发布卡的通道）
    c0 = _draft_edit_p1(client, req_id)
    r0 = client.post(f"/api/requirements/{req_id}/changes/{c0['id']}/confirm", json={"confirmedBy": "x"})
    assert r0.status_code == 409 and "窗口 1" in r0.json()["error"]

    _publish_card(client, repos, req_id)
    c1 = _draft_edit_p1(client, req_id, title="第一单")["id"]
    # digest.pushed_at 回拨一秒（秒级时间戳下发布链 push 与 confirm 同秒会命中已消化边界——
    # §5.1 严格小于口径：同秒算消化）
    db.execute("UPDATE artifact SET pushed_at='2026-10-13 00:00:00'"
               " WHERE req_id=? AND type='digest'", (req_id,))
    assert client.post(f"/api/requirements/{req_id}/changes/{c1}/confirm", json={"confirmedBy": "李四"}).status_code == 200

    # 串行守卫：前序 confirmed 单影响面未消化（digest.pushed_at < confirmed_at）拒第二单
    c2 = client.post(f"/api/requirements/{req_id}/changes", json={
        "title": "第二单", "proposedBy": "张三", "blocks": [
            {"id": "t1", "type": "title", "text": "标题"},
            {"id": "h1", "type": "heading", "level": 2, "text": "背景"},
            {"id": "p1", "type": "paragraph", "text": "一段说明（修订二）"},
            {"type": "paragraph", "text": "变更新增段"},
            {"id": "li1", "type": "list-item", "marker": "1. ", "depth": 1, "text": "列表项一"},
            {"id": "li2", "type": "list-item", "marker": "a. ", "depth": 2, "parent": "li1", "text": "子项 a"},
        ]}).json()["id"]
    r2 = client.post(f"/api/requirements/{req_id}/changes/{c2}/confirm", json={"confirmedBy": "李四"})
    assert r2.status_code == 409 and "未消化" in r2.json()["error"]

    # 消化 = digest.pushed_at 刷新到 confirm 之后（v4-14 增量合并 artifact.upsert 的真实语义；
    # 全量重推不可作模拟通道——push 的隐式推进会把 changed 吞成 clarifying，§5.1 点名的面）
    db.execute("UPDATE artifact SET pushed_at='2099-01-01 00:00:00'"
               " WHERE req_id=? AND type='digest'", (req_id,))
    # 第二单放行；changed 下 confirm 状态域 OK（不再转态）
    # changed 拒全量重推（§5.1 矩矩——v4-15 落地）？本批未落：push 在 changed 下仍可行（走旧守卫）
    # ——重推后串行判定放行的前提是 digest.pushed_at > confirmed_at，上式 push 成功即满足。
    r3 = client.post(f"/api/requirements/{req_id}/changes/{c2}/confirm", json={"confirmedBy": "王五"})
    assert r3.status_code == 200
    card = client.get(f"/api/requirements/{req_id}").json()["card"]
    assert card["status"] == "changed" and card["contentRev"] == 2
