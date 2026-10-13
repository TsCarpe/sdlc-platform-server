"""v4-8 验收：评论系统 API——块级/卡片级 CRUD、线程级解决、升级澄清项（L 系）。

契约（DESIGN-v4 §4）：评论任何阶段可发（无状态守卫）；升级是显式动作——L<两位> 编号、
origin='local'、quote=块文本、anchor=块 ID、src_comment_id 回链；已解决评论仍可升级；
升级幂等（同评论重复升级 409 不重复插行）；P/L 两族编号 UNIQUE(req_id,seq) 天然不冲突。
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


@pytest.fixture()
def env(tmp_path: Path) -> Iterator[tuple[TestClient, Database, Repos]]:
    settings = Settings(_env_file=None, db_path=str(tmp_path / "data" / "platform.db"),  # pyright: ignore[reportCallIssue]
                        media_dir=str(tmp_path / "data" / "media"))
    app = create_app(settings, cwd=tmp_path)
    client = TestClient(app, raise_server_exceptions=False)
    db = Database(tmp_path / "data" / "platform.db")
    repos = Repos.create(db)
    yield client, db, repos


def _mk_card(client: TestClient, name: str = "评论系统验证") -> str:
    created = client.post("/api/requirements", json={"name": name, "sourceType": "text",
                                                     "sourceText": "# 原文", "submittedBy": "tester"}).json()
    return str(created["id"])


def _seed_block(repos: Repos, req_id: str) -> None:
    repos.req_block.replace_all(req_id, [
        ReqBlock(req_id=req_id, block_id="c-h1", seq=1000, type="heading", text="背景",
                 attrs_json='{"level":2}'),
        ReqBlock(req_id=req_id, block_id="c-p1", seq=2000, type="paragraph", text="一段说明"),
    ])


def _post_comment(client: TestClient, req_id: str, **kw: object) -> Any:
    return client.post(f"/api/requirements/{req_id}/comments", json=kw)


# ---------- CRUD ----------

def test_comment_crud_and_validation(env: tuple[TestClient, Database, Repos]) -> None:
    client, _, repos = env
    req_id = _mk_card(client)
    _seed_block(repos, req_id)

    # text 必填；blockId 不存在 400；replyTo 不存在 400
    assert _post_comment(client, req_id, author="张三").status_code == 400
    assert _post_comment(client, req_id, text="x", blockId="GHOST").status_code == 400
    assert _post_comment(client, req_id, text="x", replyTo=999).status_code == 400

    # 块级 + 卡片级 + 回复链
    r1 = _post_comment(client, req_id, blockId="c-p1", author="张三", text="这里要改")
    assert r1.status_code == 200
    c1 = r1.json()
    assert c1["blockId"] == "c-p1" and c1["origin"] == "platform" and c1["resolved"] is False
    c2 = _post_comment(client, req_id, blockId="c-p1", author="李四", text="同意",
                       replyTo=c1["id"]).json()
    c3 = _post_comment(client, req_id, author="王五", text="整体疑问：范围边界？").json()  # 卡片级
    assert c3["blockId"] == "" and c2["replyTo"] == c1["id"]

    # 列表 + 未解决计数
    listed = client.get(f"/api/requirements/{req_id}/comments").json()
    assert [c["id"] for c in listed["comments"]] == [c1["id"], c2["id"], c3["id"]]
    assert listed["unresolvedCount"] == 3

    # 删除单条（回复行保留）
    assert client.delete(f"/api/requirements/{req_id}/comments/{c2['id']}").json() == {"ok": True}
    after = client.get(f"/api/requirements/{req_id}/comments").json()
    assert [c["id"] for c in after["comments"]] == [c1["id"], c3["id"]]
    assert client.delete(f"/api/requirements/{req_id}/comments/{c2['id']}").status_code == 404

    # 需求不存在 404
    assert client.get("/api/requirements/REQ-NOPE/comments").status_code == 404


def test_resolve_is_thread_level(env: tuple[TestClient, Database, Repos]) -> None:
    """解决是线程级（根+回复链全置，对齐飞书）；叶子目标也归根整链；重开同理；幂等。"""
    client, _, repos = env
    req_id = _mk_card(client)
    _seed_block(repos, req_id)
    c1 = _post_comment(client, req_id, blockId="c-p1", author="张三", text="根").json()
    c2 = _post_comment(client, req_id, blockId="c-p1", author="李四", text="回复一",
                       replyTo=c1["id"]).json()
    c3 = _post_comment(client, req_id, blockId="c-p1", author="王五", text="回复二",
                       replyTo=c2["id"]).json()
    other = _post_comment(client, req_id, blockId="c-h1", author="赵六", text="另一线程").json()

    # 从叶子 resolve → 整链置位，他线程不动
    assert client.post(f"/api/requirements/{req_id}/comments/{c3['id']}/resolve",
                       json={"resolved": True}).json() == {"ok": True}
    rows = {c["id"]: c for c in client.get(f"/api/requirements/{req_id}/comments").json()["comments"]}
    assert all(rows[i]["resolved"] for i in (c1["id"], c2["id"], c3["id"]))
    assert rows[other["id"]]["resolved"] is False
    assert client.get(f"/api/requirements/{req_id}/comments").json()["unresolvedCount"] == 1

    # 幂等 + 重开（回 false）
    client.post(f"/api/requirements/{req_id}/comments/{c1['id']}/resolve", json={"resolved": True})
    client.post(f"/api/requirements/{req_id}/comments/{c1['id']}/resolve", json={"resolved": False})
    rows2 = {c["id"]: c for c in client.get(f"/api/requirements/{req_id}/comments").json()["comments"]}
    assert not any(rows2[i]["resolved"] for i in (c1["id"], c2["id"], c3["id"]))
    assert client.post(f"/api/requirements/{req_id}/comments/999/resolve",
                       json={"resolved": True}).status_code == 404


# ---------- 升级澄清项（L 系） ----------

def test_escalate_l_series(env: tuple[TestClient, Database, Repos]) -> None:
    client, db, repos = env
    req_id = _mk_card(client)
    _seed_block(repos, req_id)
    # P 系先在（两族并存验证）
    client.post(f"/api/requirements/{req_id}/artifacts", json={
        "digest": {"md": "", "summary": "s", "roles": [], "features": [], "flows": [], "states": []},
        "audit": {"md": "", "red": 0, "yellow": 0, "blue": 0, "items": []},
        "items": [{"seq": "P1", "level": "yellow", "theme": "t", "question": "q1"},
                  {"seq": "P2", "level": "yellow", "theme": "t", "question": "q2"}],
    })

    blk = _post_comment(client, req_id, blockId="c-p1", author="张三", text="这句口径有歧义").json()
    card = _post_comment(client, req_id, author="王五", text="整体边界不清").json()

    e1 = client.post(f"/api/requirements/{req_id}/comments/{blk['id']}/escalate", json={}).json()
    # L01 起、quote=块文本、anchor=块 ID、origin=local、srcCommentId 回链、question=评论文本
    assert e1 == {"seq": "L01", "level": "yellow", "theme": "评论升级", "question": "这句口径有歧义",
                  "quote": "一段说明", "anchor": "c-p1", "status": "pending",
                  "origin": "local", "srcCommentId": blk["id"]}
    # 卡片级：quote/anchor 皆空
    e2 = client.post(f"/api/requirements/{req_id}/comments/{card['id']}/escalate",
                     json={"level": "blue", "theme": "边界"}).json()
    assert e2["seq"] == "L02" and e2["anchor"] == "" and e2["quote"] == ""
    assert e2["level"] == "blue" and e2["theme"] == "边界"

    # 两族并存：P1/P2/L01/L02 共存（UNIQUE(req_id,seq) 天然不冲突），answers 面可见
    answers = client.get(f"/api/requirements/{req_id}/answers").json()
    assert [a["seq"] for a in answers] == ["P1", "P2", "L01", "L02"]
    row = db.query_one("SELECT * FROM clarify_item WHERE req_id=? AND seq='L01'", (req_id,))
    assert row is not None and row["origin"] == "local" and row["src_comment_id"] == blk["id"]

    # 幂等：同评论重复升级 409，不重复插行
    r = client.post(f"/api/requirements/{req_id}/comments/{blk['id']}/escalate", json={})
    assert r.status_code == 409 and "只可升级一次" in r.json()["error"]
    assert db.query_scalar("SELECT COUNT(*) FROM clarify_item WHERE req_id=? AND seq LIKE 'L%'",
                           (req_id,)) == 2

    # 已解决评论仍可升级（§4.3：已结束的讨论仍可能需要正式追踪），且不改评论 resolved 态
    client.post(f"/api/requirements/{req_id}/comments/{blk['id']}/resolve", json={"resolved": True})
    e3_target = _post_comment(client, req_id, blockId="c-h1", author="赵六", text="标题歧义").json()
    client.post(f"/api/requirements/{req_id}/comments/{e3_target['id']}/resolve", json={"resolved": True})
    e3 = client.post(f"/api/requirements/{req_id}/comments/{e3_target['id']}/escalate", json={})
    assert e3.status_code == 200 and e3.json()["seq"] == "L03"
    rows3 = {c["id"]: c for c in client.get(f"/api/requirements/{req_id}/comments").json()["comments"]}
    assert rows3[e3_target["id"]]["resolved"] is True

    # 非法 level → 400；评论不存在 → 404
    bad = _post_comment(client, req_id, author="x", text="y").json()
    assert client.post(f"/api/requirements/{req_id}/comments/{bad['id']}/escalate",
                       json={"level": "pink"}).status_code == 400
    assert client.post(f"/api/requirements/{req_id}/comments/999/escalate",
                       json={}).status_code == 404


def test_escalate_on_soft_deleted_block(env: tuple[TestClient, Database, Repos]) -> None:
    """软删块上的评论升级：quote 仍带入块文本（§4.3 信息不丢——块行软删不物理删）。"""
    client, _, repos = env
    req_id = _mk_card(client)
    _seed_block(repos, req_id)
    cm = _post_comment(client, req_id, blockId="c-p1", author="张三", text="疑问").json()

    # 编辑面删掉 c-p1（软删）
    client.put(f"/api/requirements/{req_id}/blocks", json={
        "updatedBy": "dev", "blocks": [{"id": "c-h1", "type": "heading", "level": 2, "text": "背景"}]})
    e = client.post(f"/api/requirements/{req_id}/comments/{cm['id']}/escalate", json={}).json()
    assert e["anchor"] == "c-p1" and e["quote"] == "一段说明"


def test_comment_and_escalate_on_published(env: tuple[TestClient, Database, Repos]) -> None:
    """published 卡：评论随时可发（§4.1 自由讨论），升级也开放（处置走三期变更单通道）。
    注：push_artifacts 现行全量替换会把 local 行冲掉——按来源合并保护是 v4-9 的货，本测试
    在推产物前完成升级、发布链不与 L 行交叠。"""
    client, db, repos = env
    req_id = _mk_card(client)
    _seed_block(repos, req_id)
    # 先升级（L01），再推产物发布——发布链的 push 会重建机器行（P 系），L01 断言在 push 前做
    cm = _post_comment(client, req_id, blockId="c-p1", author="张三", text="发布前疑问").json()
    assert client.post(f"/api/requirements/{req_id}/comments/{cm['id']}/escalate",
                       json={}).json()["seq"] == "L01"
    client.post(f"/api/requirements/{req_id}/artifacts", json={
        "digest": {"md": "", "summary": "s", "roles": [], "features": [], "flows": [], "states": []},
        "audit": {"md": "", "red": 0, "yellow": 0, "blue": 0, "items": []},
        "items": [{"seq": "P1", "level": "yellow", "theme": "t", "question": "q"}]})
    client.put(f"/api/requirements/{req_id}/answers/P1", json={"answer": "a"})
    client.post(f"/api/requirements/{req_id}/publish", json={"standardMd": "# x"})

    # published：评论可发、可解决、可升级（漏网问题 → 三期变更单通道处置）
    pub_cm = _post_comment(client, req_id, author="李四", text="发布后漏网问题").json()
    assert pub_cm["id"] > 0
    pub_e = client.post(f"/api/requirements/{req_id}/comments/{pub_cm['id']}/escalate", json={})
    assert pub_e.status_code == 200
    assert db.query_scalar("SELECT status FROM requirement WHERE id=?", (req_id,)) == "published"
