"""v4-2 验收：块模型读侧——{blocks,media} 同形状组装、doc.json/comments.json 端点、detail.doc 切源回退。

消费兼容契约（DESIGN-v4 §2.3/§3）：doc_payload 与 parse_doc 输出同构（平铺块 + path/ordinal 读时推导），
comments_payload 与 resolve_anchors --comments 契约同构（数组 + comment_id 键）。
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
from platform_sdlc.models import MediaFile, ReqBlock, ReqComment
from platform_sdlc.repositories import Repos
from platform_sdlc.services.block import BlockService, doc_text_stream, normalize_text_fingerprint


def _guard_for(doc: dict[str, Any]) -> dict[str, Any]:
    """POST /artifacts 推 doc 夹具挂合法 guard（守恒终检前置校验会拒无 guard 的 doc）：
    textSha 用平台同款算法现算；本夹具无媒体，mediaTokens 恒空。"""
    return {"algo": "conservation-v1", "source": "xml", "mediaTokens": [],
            "textSha": normalize_text_fingerprint(doc_text_stream(doc["blocks"]))}


@pytest.fixture()
def env(tmp_path: Path) -> Iterator[tuple[TestClient, Database, Repos, BlockService]]:
    settings = Settings(_env_file=None, db_path=str(tmp_path / "data" / "platform.db"),  # pyright: ignore[reportCallIssue]
                        media_dir=str(tmp_path / "data" / "media"))
    app = create_app(settings, cwd=tmp_path)
    client = TestClient(app, raise_server_exceptions=False)
    db = Database(tmp_path / "data" / "platform.db")
    repos = Repos.create(db)
    blocks = BlockService(db, repos, tmp_path)
    yield client, db, repos, blocks


def _seed_blocks(repos: Repos, req_id: str) -> None:
    repos.req_block.replace_all(req_id, [
        ReqBlock(req_id=req_id, block_id="fs-title", seq=1000, type="title", text="需求标题"),
        ReqBlock(req_id=req_id, block_id="fs-h1", seq=2000, type="heading", text="背景",
                 attrs_json='{"level":2}'),
        ReqBlock(req_id=req_id, block_id="fs-p1", seq=3000, type="paragraph", text="一段说明"),
        ReqBlock(req_id=req_id, block_id="fs-li1", seq=4000, type="list-item", text="列表项一",
                 attrs_json='{"marker":"1. ","depth":1,"parent":"fs-p0"}'),
        ReqBlock(req_id=req_id, block_id="fs-li2", seq=5000, type="list-item", text="子项 a",
                 attrs_json='{"marker":"a. ","depth":2,"parent":"fs-li1"}'),
        ReqBlock(req_id=req_id, block_id="fs-img1", seq=6000, type="image",
                 attrs_json='{"media":"m-0001","name":"proto.png"}'),
    ])


# ---------- 服务层：doc_payload 形状 ----------

def test_doc_payload_shape(env: tuple[TestClient, Database, Repos, BlockService]) -> None:
    _, _, repos, blocks = env
    _seed_blocks(repos, "REQ-2026-001")
    repos.media.insert(MediaFile(req_id="REQ-2026-001", media_id="m-0001", sha256="x" * 64,
                                 name="proto.png", mime="image/png", bytes=70, created_at=None))

    payload = blocks.doc_payload("REQ-2026-001")
    assert payload is not None
    out = payload["blocks"]
    by_id = {b["id"]: b for b in out}
    # 平铺同构：id/type/text + attrs 顶层展开
    assert by_id["fs-h1"] == {"id": "fs-h1", "type": "heading", "text": "背景", "level": 2, "path": "背景"}
    # path 推导：paragraph 取最近 heading
    assert by_id["fs-p1"]["path"] == "背景"
    # ordinal 推导：marker 清理段按 parent 链拼接（父为非 list-item 时重起）
    assert by_id["fs-li1"]["ordinal"] == "1"
    assert by_id["fs-li2"]["ordinal"] == "1.a"
    assert by_id["fs-li1"]["path"] == "背景 › 1"
    # media 数组从 media 表组装（id/name/mime/bytes/sha256）
    assert payload["media"] == [{"id": "m-0001", "name": "proto.png", "mime": "image/png",
                                 "bytes": 70, "sha256": "x" * 64}]
    # 空块表 → None（调用方回退）
    assert blocks.doc_payload("REQ-NONE") is None


def test_doc_payload_comment_threads(env: tuple[TestClient, Database, Repos, BlockService]) -> None:
    _, _, repos, blocks = env
    _seed_blocks(repos, "REQ-2026-001")
    root = repos.req_comment.insert(ReqComment(
        id=0, req_id="REQ-2026-001", block_id="fs-p1", origin="feishu", external_id="7280001",
        author="张三", text="这里要改"))
    repos.req_comment.insert(ReqComment(
        id=0, req_id="REQ-2026-001", block_id="fs-p1", origin="feishu", external_id="7280001",
        author="李四", text="同意", reply_to=root))
    repos.req_comment.insert(ReqComment(
        id=0, req_id="REQ-2026-001", block_id="fs-h1", origin="platform", author="王五", text="标题有歧义"))
    # 宿主软删的评论：追加尾部不丢
    repos.req_comment.insert(ReqComment(
        id=0, req_id="REQ-2026-001", block_id="fs-gone", origin="platform", author="赵六", text="已删块上的疑问"))

    out = blocks.doc_payload("REQ-2026-001")
    assert out is not None
    blocks_out = out["blocks"]
    by_id = {b["id"]: b for b in blocks_out}
    # comment 块插宿主后：thread 摊平、anchorBlockId 回链、ref 按宿主文档序编号
    # （fs-h1 seq 在 fs-p1 前 → 王五组先编 c1，张三组 c2——对齐 parse_doc 的文档序 ref）
    assert by_id["7280001"] == {"id": "7280001", "type": "comment", "ref": "c2", "anchorBlockId": "fs-p1",
                                "quote": "", "thread": [{"user": "张三", "text": "这里要改"},
                                                        {"user": "李四", "text": "同意"}],
                                "reactions": None, "path": "评论区"}
    # 宿主块补 comment-refs
    assert by_id["fs-p1"]["comments"] == "c2"
    assert by_id["fs-h1"]["comments"] == "c1"
    # platform 评论以行 id 代 comment-id；软删宿主评论追加尾部
    tail = blocks_out[-1]
    assert tail["anchorBlockId"] == "fs-gone" and tail["thread"] == [{"user": "赵六", "text": "已删块上的疑问"}]
    # 块序：comment 紧随宿主
    ids = [b["id"] for b in blocks_out]
    assert ids.index("7280001") == ids.index("fs-p1") + 1


def test_comments_payload_compat(env: tuple[TestClient, Database, Repos, BlockService]) -> None:
    _, _, repos, blocks = env
    _seed_blocks(repos, "REQ-2026-001")
    repos.req_comment.insert(ReqComment(
        id=0, req_id="REQ-2026-001", block_id="fs-p1", origin="feishu", external_id="7280001",
        author="张三", text="q"))
    out = blocks.comments_payload("REQ-2026-001")
    # resolve_anchors --comments 契约：数组 + comment_id（按序号 → comment-id 映射）
    assert out[0]["comment_id"] == "7280001"
    assert {"block_id", "author", "text", "reply_to", "resolved", "created_at", "origin"} <= set(out[0])


# ---------- 服务层：detail.doc 切源与回退 ----------

def test_doc_view_and_detail_fallback(env: tuple[TestClient, Database, Repos, BlockService]) -> None:
    client, _, repos, blocks = env
    created = client.post("/api/requirements", json={"name": "切源验证", "sourceType": "text",
                                                     "sourceText": "# 原文"}).json()
    req_id = created["id"]
    # 块表空 + doc artifact 存在 → 回退 artifact（verify-m4 夹具同款路径）
    doc_fixture: dict[str, Any] = {
        "generator": "parse_doc.py 0.2", "source": {"url": "", "documentId": "m4", "revisionId": 1},
        "blocks": [{"id": "doxcnH1", "type": "heading", "level": 2, "text": "详述", "path": "详述"}],
        "media": []}
    client.post(f"/api/requirements/{req_id}/artifacts", json={
        "digest": {"md": "", "summary": "s", "roles": [], "features": [], "flows": [], "states": []},
        "audit": {"md": "", "red": 0, "yellow": 0, "blue": 0, "items": []},
        "items": [{"seq": "P1", "level": "yellow", "theme": "t", "question": "q"}],
        "doc": {**doc_fixture, "guard": _guard_for(doc_fixture)},
    })
    detail = client.get(f"/api/requirements/{req_id}").json()
    assert detail["doc"]["content"]["source"]["documentId"] == "m4"  # 回退路径保留 artifact 原形状
    assert blocks.doc_view(req_id) is None

    # 块表落行后 → 切源权威（content 从块表组装，version/pushedAt 聚合）
    repos.req_block.replace_all(req_id, [
        ReqBlock(req_id=req_id, block_id="fs-1", seq=1000, type="paragraph", text="权威内容")])
    detail2 = client.get(f"/api/requirements/{req_id}").json()
    assert detail2["doc"]["content"]["blocks"][0]["id"] == "fs-1"
    assert detail2["doc"]["markdown"] == "" and detail2["doc"]["version"] == 1


def test_artifacts_doc_without_guard_rejected(env: tuple[TestClient, Database, Repos, BlockService]) -> None:
    """守恒门不受 approve 剥除影响：HTTP 直推（sdlc-sync 外部导入通道）带无 guard 的 doc
    → 409 拒（剥除只发生在质检草稿采纳链，外部导入通道仍全量终检）。"""
    client, _, _, _ = env
    created = client.post("/api/requirements", json={"name": "守恒门直推验证", "sourceType": "text",
                                                     "sourceText": "# 原文"}).json()
    req_id = created["id"]
    r = client.post(f"/api/requirements/{req_id}/artifacts", json={
        "digest": {"md": "", "summary": "s", "roles": [], "features": [], "flows": [], "states": []},
        "audit": {"md": "", "red": 0, "yellow": 0, "blue": 0, "items": []},
        "items": [{"seq": "P1", "level": "yellow", "theme": "t", "question": "q"}],
        "doc": {"blocks": [{"id": "x", "type": "paragraph", "text": "无 guard"}], "media": []},
    })
    assert r.status_code == 409
    assert "缺守恒参照（guard）" in r.json()["error"]


# ---------- HTTP：doc.json / comments.json 端点 ----------

def test_doc_json_endpoints(env: tuple[TestClient, Database, Repos, BlockService]) -> None:
    client, _, repos, _ = env
    created = client.post("/api/requirements", json={"name": "导出验证", "sourceType": "text",
                                                     "sourceText": "# 原文"}).json()
    req_id = created["id"]

    # 两者皆无 → 404 引导
    r = client.get(f"/api/requirements/{req_id}/doc.json")
    assert r.status_code == 404 and "先完成 source_import" in r.json()["error"]
    # 需求不存在 → 404
    assert client.get("/api/requirements/REQ-NOPE/doc.json").status_code == 404
    # comments.json 空数组
    assert client.get(f"/api/requirements/{req_id}/comments.json").json() == []

    # 块表落行 → 200 同形状
    repos.req_block.replace_all(req_id, [
        ReqBlock(req_id=req_id, block_id="fs-1", seq=1000, type="paragraph", text="a")])
    doc = client.get(f"/api/requirements/{req_id}/doc.json").json()
    assert doc["blocks"][0]["id"] == "fs-1" and "media" in doc
