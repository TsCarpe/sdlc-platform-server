"""v4-7 验收：窗口 1 块级直接编辑——服务端 diff 四态（LIS 判 move）、事务应用
（version+1 / blk-<ulid> / 软删 / 整体重排 seq / 行内子块约束）、双守卫（拒 published、活跃质检互斥）。

契约（DESIGN-v4 §5.3/§1 表）：前端提交全量块数组，diff 只在服务端一份；挪 1 块只 1 move
（朴素逐位对比会把一次挪动判成 N 个 move，把无关行拉进影响面）。
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from platform_sdlc.config import Settings
from platform_sdlc.db import Database
from platform_sdlc.main import create_app
from platform_sdlc.models import ReqBlock
from platform_sdlc.repositories import Repos

_CROCKFORD = re.compile(r"^[0-9A-HJKMNP-TV-Z]{26}$")  # 26 字符 ULID（无 I/L/O/U）


@pytest.fixture()
def env(tmp_path: Path) -> Iterator[tuple[TestClient, Database, Repos]]:
    settings = Settings(_env_file=None, db_path=str(tmp_path / "data" / "platform.db"),  # pyright: ignore[reportCallIssue]
                        media_dir=str(tmp_path / "data" / "media"))
    app = create_app(settings, cwd=tmp_path)
    client = TestClient(app, raise_server_exceptions=False)
    db = Database(tmp_path / "data" / "platform.db")
    repos = Repos.create(db)
    yield client, db, repos


def _mk_card(client: TestClient, name: str = "块级编辑验证") -> str:
    created = client.post("/api/requirements", json={"name": name, "sourceType": "text",
                                                     "sourceText": "# 原文", "submittedBy": "tester"}).json()
    return str(created["id"])


def _seed(repos: Repos, req_id: str) -> None:
    """4 块基线：h1 + p1 + li1（顶层项）+ li2（li1 子项）。"""
    repos.req_block.replace_all(req_id, [
        ReqBlock(req_id=req_id, block_id="e-h1", seq=1000, type="heading", text="背景",
                 attrs_json='{"level":2}'),
        ReqBlock(req_id=req_id, block_id="e-p1", seq=2000, type="paragraph", text="一段说明"),
        ReqBlock(req_id=req_id, block_id="e-li1", seq=3000, type="list-item", text="列表项一",
                 attrs_json='{"marker":"1. ","depth":1,"parent":null}'),
        ReqBlock(req_id=req_id, block_id="e-li2", seq=4000, type="list-item", text="子项 a",
                 attrs_json='{"marker":"a. ","depth":2,"parent":"e-li1"}'),
    ])


# ---------- diff 四态 + LIS ----------

def test_preview_diff_four_ops(env: tuple[TestClient, Database, Repos]) -> None:
    client, _, repos = env
    req_id = _mk_card(client)
    _seed(repos, req_id)

    # li1 挪最前（move）、p1 改文本（edit）、li2 不动、h1 不提交（delete）、新段落（insert 空 id）
    preview = client.post(f"/api/requirements/{req_id}/blocks/diff", json={
        "updatedBy": "dev",
        "blocks": [
            {"id": "e-li1", "type": "list-item", "marker": "1. ", "depth": 1, "text": "列表项一"},
            {"id": "e-p1", "type": "paragraph", "text": "一段说明（修订）"},
            {"id": "e-li2", "type": "list-item", "marker": "a. ", "depth": 2, "parent": "e-li1", "text": "子项 a"},
            {"type": "paragraph", "text": "补充说明"},
        ],
    })
    assert preview.status_code == 200
    out = preview.json()
    # h1(1000) 消失 = delete；li1(3000) 挪到最前、LIS 于 [3000,2000,4000] 判 li1 唯一动序 = move；
    # p1 文本变 = edit；空 id = insert（blockId 为提交临时 id——空）
    assert out["counts"] == {"edit": 1, "insert": 1, "delete": 1, "move": 1}
    by_op = {it["op"]: it for it in out["items"]}
    assert by_op["edit"] == {"blockId": "e-p1", "op": "edit", "oldText": "一段说明", "newText": "一段说明（修订）",
                             "oldAttrs": {}, "newAttrs": {}}
    assert by_op["move"]["blockId"] == "e-li1" and by_op["move"]["oldText"] == "列表项一"
    assert by_op["delete"]["blockId"] == "e-h1" and by_op["delete"]["oldText"] == "背景"
    assert by_op["insert"] == {"blockId": "", "op": "insert", "oldText": "", "newText": "补充说明",
                               "oldAttrs": None, "newAttrs": {}}
    # 预览 dry-run：块表零变化
    assert [b.block_id for b in repos.req_block.list_by_req(req_id)] == ["e-h1", "e-p1", "e-li1", "e-li2"]


def test_lis_single_move_only_one_item(env: tuple[TestClient, Database, Repos]) -> None:
    """[A,B,C,D]→[A,C,D,B]：朴素逐位对比判 3 个 move，LIS 只判 B——「挪 1 块只 1 move」。"""
    client, _, repos = env
    req_id = _mk_card(client)
    _seed(repos, req_id)
    out = client.put(f"/api/requirements/{req_id}/blocks", json={
        "updatedBy": "dev",
        "blocks": [
            {"id": "e-h1", "type": "heading", "level": 2, "text": "背景"},
            {"id": "e-li1", "type": "list-item", "marker": "1. ", "depth": 1, "text": "列表项一"},
            {"id": "e-li2", "type": "list-item", "marker": "a. ", "depth": 2, "parent": "e-li1", "text": "子项 a"},
            {"id": "e-p1", "type": "paragraph", "text": "一段说明"},
        ],
    }).json()
    assert out["counts"] == {"edit": 0, "insert": 0, "delete": 0, "move": 1}
    assert [it["blockId"] for it in out["items"]] == ["e-p1"]


def test_apply_effects(env: tuple[TestClient, Database, Repos]) -> None:
    client, db, repos = env
    req_id = _mk_card(client)
    _seed(repos, req_id)

    out = client.put(f"/api/requirements/{req_id}/blocks", json={
        "updatedBy": "张三",
        "blocks": [
            {"id": "e-li1", "type": "list-item", "marker": "1. ", "depth": 1, "text": "列表项一（改）"},
            {"id": "e-p1", "type": "paragraph", "text": "一段说明"},
            {"id": "e-li2", "type": "list-item", "marker": "a. ", "depth": 2, "parent": "e-li1",
             "text": "子项 a"},
        ],
    }).json()
    assert out["counts"] == {"edit": 1, "insert": 0, "delete": 1, "move": 0}

    rows = {r["block_id"]: r for r in db.query_all(
        "SELECT * FROM req_block WHERE req_id=? ORDER BY seq", (req_id,))}
    # e-h1 软删留痕（deleted=时间戳），读面不可见
    assert rows["e-h1"]["deleted"] != "" and rows["e-h1"]["updated_by"] == "张三"
    assert [b.block_id for b in repos.req_block.list_by_req(req_id)] == ["e-li1", "e-p1", "e-li2"]
    # edit 块 version+1 + 文本/留痕；整体重排 seq=(i+1)*1000
    assert rows["e-li1"]["version"] == 2 and rows["e-li1"]["text"] == "列表项一（改）"
    assert rows["e-li1"]["updated_by"] == "张三"
    assert [rows[k]["seq"] for k in ("e-li1", "e-p1", "e-li2")] == [1000, 2000, 3000]

    # doc.json 读面联动：软删 h1 不渲染
    doc = client.get(f"/api/requirements/{req_id}/doc.json").json()
    assert [b["id"] for b in doc["blocks"]] == ["e-li1", "e-p1", "e-li2"]

    # 幂等：同数组重发 diff 为空、零写入（version 不再 +1）
    again = client.put(f"/api/requirements/{req_id}/blocks", json={
        "updatedBy": "李四",
        "blocks": [
            {"id": "e-li1", "type": "list-item", "marker": "1. ", "depth": 1, "text": "列表项一（改）"},
            {"id": "e-p1", "type": "paragraph", "text": "一段说明"},
            {"id": "e-li2", "type": "list-item", "marker": "a. ", "depth": 2, "parent": "e-li1",
             "text": "子项 a"},
        ],
    }).json()
    assert again["items"] == [] and again["counts"] == {"edit": 0, "insert": 0, "delete": 0, "move": 0}
    rows2 = {r["block_id"]: r for r in db.query_all("SELECT * FROM req_block WHERE req_id=?", (req_id,))}
    assert rows2["e-li1"]["version"] == 2 and rows2["e-li1"]["updated_by"] == "张三"


def test_insert_gets_blk_ulid_and_parent_remap(env: tuple[TestClient, Database, Repos]) -> None:
    """insert 服务端发 blk-<ulid>（26 字符 Crockford Base32）；新表格行（临时 id）+ 行内子块
    （parent 引用临时 id）同批落库时映射改写、子块紧随父块。"""
    client, db, repos = env
    req_id = _mk_card(client)
    repos.req_block.replace_all(req_id, [
        ReqBlock(req_id=req_id, block_id="e-p1", seq=1000, type="paragraph", text="基线")])

    out = client.put(f"/api/requirements/{req_id}/blocks", json={
        "updatedBy": "dev",
        "blocks": [
            {"id": "e-p1", "type": "paragraph", "text": "基线"},
            {"id": "tmp-row", "type": "table-row", "head": ["列A"], "cells": [["v1"]]},
            {"id": "tmp-cell", "type": "paragraph", "parent": "tmp-row", "text": "行内块"},
        ],
    }).json()
    inserts = [it for it in out["items"] if it["op"] == "insert"]
    row_id = next(it["blockId"] for it in inserts if it["newText"] == "")
    cell_id = next(it["blockId"] for it in inserts if it["newText"] == "行内块")
    for bid in (row_id, cell_id):
        assert bid.startswith("blk-") and _CROCKFORD.match(bid[4:]) and len(bid) == 30

    blocks = repos.req_block.list_by_req(req_id)
    by_id = {b.block_id: b for b in blocks}
    # 临时 parent 引用已改写为正式 blk- ID；行内子块 seq 紧随父块（§2.3 写入约束）
    import json as _json
    assert _json.loads(by_id[cell_id].attrs_json)["parent"] == row_id
    assert by_id[cell_id].seq == by_id[row_id].seq + 1000
    # insert 行 version=1、创建留痕
    assert by_id[row_id].version == 1 and by_id[row_id].updated_by == "dev"
    assert db.query_scalar("SELECT COUNT(*) FROM req_block WHERE req_id=? AND block_id LIKE 'blk-%'",
                           (req_id,)) == 2


# ---------- 子块拓扑约束（§2.3） ----------

def test_row_child_must_follow_parent(env: tuple[TestClient, Database, Repos]) -> None:
    client, _, repos = env
    req_id = _mk_card(client)
    repos.req_block.replace_all(req_id, [
        ReqBlock(req_id=req_id, block_id="e-p1", seq=1000, type="paragraph", text="基线"),
        ReqBlock(req_id=req_id, block_id="e-row", seq=2000, type="table-row", text="",
                 attrs_json='{"head":["列A"],"cells":[["v1"]]}'),
        ReqBlock(req_id=req_id, block_id="e-cell", seq=3000, type="paragraph", text="行内块",
                 attrs_json='{"parent":"e-row"}'),
    ])

    # 子块挪到父块之前（前向引用）→ 400
    r1 = client.put(f"/api/requirements/{req_id}/blocks", json={
        "updatedBy": "dev",
        "blocks": [{"id": "e-cell", "type": "paragraph", "parent": "e-row", "text": "行内块"},
                   {"id": "e-row", "type": "table-row", "head": ["列A"], "cells": [["v1"]]},
                   {"id": "e-p1", "type": "paragraph", "text": "基线"}]})
    assert r1.status_code == 400 and "未在本批之前出现" in r1.json()["error"]

    # 行内子块被隔开（非紧随）→ 400
    r2 = client.put(f"/api/requirements/{req_id}/blocks", json={
        "updatedBy": "dev",
        "blocks": [{"id": "e-row", "type": "table-row", "head": ["列A"], "cells": [["v1"]]},
                   {"id": "e-p1", "type": "paragraph", "text": "基线"},
                   {"id": "e-cell", "type": "paragraph", "parent": "e-row", "text": "行内块"}]})
    assert r2.status_code == 400 and "必须紧随其表格行" in r2.json()["error"]

    # 删父留子（parent 悬空）→ 400
    r3 = client.put(f"/api/requirements/{req_id}/blocks", json={
        "updatedBy": "dev",
        "blocks": [{"id": "e-p1", "type": "paragraph", "text": "基线"},
                   {"id": "e-cell", "type": "paragraph", "parent": "e-row", "text": "行内块"}]})
    assert r3.status_code == 400 and "父块缺失" in r3.json()["error"]

    # 连续两个同父子块合法（紧跟父块或前一个同父子块）
    r4 = client.put(f"/api/requirements/{req_id}/blocks", json={
        "updatedBy": "dev",
        "blocks": [{"id": "e-p1", "type": "paragraph", "text": "基线"},
                   {"id": "e-row", "type": "table-row", "head": ["列A"], "cells": [["v1"]]},
                   {"id": "e-cell", "type": "paragraph", "parent": "e-row", "text": "行内块"},
                   {"type": "paragraph", "parent": "e-row", "text": "行内块二"}]})
    assert r4.status_code == 200 and r4.json()["counts"]["insert"] == 1


def test_list_child_parent_must_precede(env: tuple[TestClient, Database, Repos]) -> None:
    """流式子块（list-item 子项）：parent 在前即可，不要求紧邻（§2.3 仅行内子块紧随）。"""
    client, _, repos = env
    req_id = _mk_card(client)
    _seed(repos, req_id)
    # li1 与 li2 之间插入新段落：li2 的 parent=li1 仍在前 → 合法
    r = client.put(f"/api/requirements/{req_id}/blocks", json={
        "updatedBy": "dev",
        "blocks": [
            {"id": "e-h1", "type": "heading", "level": 2, "text": "背景"},
            {"id": "e-p1", "type": "paragraph", "text": "一段说明"},
            {"id": "e-li1", "type": "list-item", "marker": "1. ", "depth": 1, "text": "列表项一"},
            {"type": "paragraph", "text": "插入段"},
            {"id": "e-li2", "type": "list-item", "marker": "a. ", "depth": 2, "parent": "e-li1",
             "text": "子项 a"},
        ]})
    assert r.status_code == 200 and r.json()["counts"] == {"edit": 0, "insert": 1, "delete": 0, "move": 0}


# ---------- 守卫 ----------

def test_edit_guards(env: tuple[TestClient, Database, Repos]) -> None:
    client, db, repos = env
    req_id = _mk_card(client)
    _seed(repos, req_id)

    # 需求不存在 → 404
    assert client.put("/api/requirements/REQ-NOPE/blocks",
                      json={"updatedBy": "x", "blocks": [{"type": "paragraph", "text": "x"}]}).status_code == 404
    # 手填身份必填（§9）
    r0 = client.put(f"/api/requirements/{req_id}/blocks",
                    json={"blocks": [{"id": "e-p1", "type": "paragraph", "text": "x"}]})
    assert r0.status_code == 400 and "updatedBy" in r0.json()["error"]
    # blocks 非空数组
    assert client.put(f"/api/requirements/{req_id}/blocks",
                      json={"updatedBy": "x", "blocks": []}).status_code == 400
    # comment 块不入块表
    assert client.put(f"/api/requirements/{req_id}/blocks",
                      json={"updatedBy": "x", "blocks": [{"id": "e-p1", "type": "comment", "text": "x"}]}
                      ).status_code == 400
    # 非法类型
    assert client.put(f"/api/requirements/{req_id}/blocks",
                      json={"updatedBy": "x", "blocks": [{"type": "quote", "text": "x"}]}).status_code == 400

    # 块表空（无基线）→ 409 引导
    empty_id = _mk_card(client, name="未导入卡")
    r1 = client.put(f"/api/requirements/{empty_id}/blocks",
                    json={"updatedBy": "x", "blocks": [{"type": "paragraph", "text": "x"}]})
    assert r1.status_code == 409 and "先完成 source_import" in r1.json()["error"]

    # 活跃质检互斥：queued quality_check → 409；cancel 解除 → 200
    q = client.post(f"/api/requirements/{req_id}/tasks", json={"type": "quality_check"}).json()
    r2 = client.put(f"/api/requirements/{req_id}/blocks", json={
        "updatedBy": "x", "blocks": [{"id": "e-p1", "type": "paragraph", "text": "一段说明"}]})
    assert r2.status_code == 409 and "质检" in r2.json()["error"]
    client.post(f"/api/tasks/{q['id']}/cancel")
    assert client.put(f"/api/requirements/{req_id}/blocks", json={
        "updatedBy": "x", "blocks": [{"id": "e-p1", "type": "paragraph", "text": "一段说明"}]}
                      ).status_code == 200

    # published 终态锁拒（§1 表：直接编辑继承的唯一谓词）
    db.execute("UPDATE requirement SET status='published' WHERE id=?", (req_id,))
    r3 = client.put(f"/api/requirements/{req_id}/blocks", json={
        "updatedBy": "x", "blocks": [{"id": "e-p1", "type": "paragraph", "text": "y"}]})
    assert r3.status_code == 409 and "终态锁定" in r3.json()["error"]

    # 软删块复活 → 409 基线过期（软删块的复活是三期放弃变更语义，不属窗口 1）
    db.execute("UPDATE requirement SET status='clarifying' WHERE id=?", (req_id,))
    other = _mk_card(client, name="基线过期验证")
    repos.req_block.replace_all(other, [ReqBlock(req_id=other, block_id="g-1", seq=1000,
                                                 type="paragraph", text="a")])
    client.put(f"/api/requirements/{other}/blocks",
               json={"updatedBy": "x", "blocks": [{"type": "paragraph", "text": "b"}]})  # g-1 软删
    r4 = client.put(f"/api/requirements/{other}/blocks",
                    json={"updatedBy": "x", "blocks": [{"id": "g-1", "type": "paragraph", "text": "a"}]})
    assert r4.status_code == 409 and "基线过期" in r4.json()["error"]
