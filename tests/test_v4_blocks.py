"""v4-1 验收：块表/评论表落库地基与存量迁移（DESIGN-v4 §2.2/§4.2/§6.3/§10）。

迁移类测试先以裸 schema 库（executescript 建表、不跑账本）造存量形态数据，再 run_migrations 触发
账本步骤——与 create_app 一把全跑的路径区分开，才能断言「存量 → 迁移后」的形状。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from platform_sdlc.db import Database
from platform_sdlc.migrations import _SCHEMA, run_migrations
from platform_sdlc.models import ClarifyItem, ReqBlock, ReqComment, Requirement
from platform_sdlc.repositories import Repos
from platform_sdlc.services.requirement import RequirementService


def _legacy_db(tmp_path: Path) -> Database:
    """裸 schema 库（建表不跑账本），供「先造存量数据再触发迁移」的测试。"""
    db = Database(tmp_path / "legacy.db")
    db.executescript(_SCHEMA)
    db.execute("INSERT INTO requirement(id,name,source_type,status) VALUES('REQ-2026-001','迁移验证','feishu','clarifying')")
    return db


def _insert_doc_artifact(db: Database, blocks: list[dict[str, object]]) -> None:
    db.execute(
        "INSERT INTO artifact(req_id,type,markdown,content_json,version) VALUES('REQ-2026-001','doc','',?,1)",
        (json.dumps({"blocks": blocks, "media": []}, ensure_ascii=False),),
    )


# ---------- 迁移：doc artifact → req_block / req_comment ----------

def test_migration_moves_doc_artifact_to_blocks(tmp_path: Path) -> None:
    db = _legacy_db(tmp_path)
    _insert_doc_artifact(db, [
        {"id": "fs-title", "type": "title", "text": "需求标题", "path": ""},
        {"id": "fs-h1", "type": "heading", "level": 2, "text": "背景", "path": "需求标题"},
        {"id": "fs-p1", "type": "paragraph", "text": "一段说明", "path": "需求标题 / 背景", "comments": "c1"},
        {"id": "fs-li1", "type": "list-item", "marker": "•", "ordinal": "1", "depth": 0,
         "parent": "fs-p1", "text": "列表项一", "path": "需求标题 / 背景"},
        {"id": "fs-img1", "type": "image", "media": "m-abc", "text": "", "path": "需求标题 / 背景"},
        {"id": "cm-1", "type": "comment", "ref": "c1", "anchorBlockId": "fs-p1", "quote": "一段说明",
         "thread": [{"user": "张三", "text": "这里要改"}, {"user": "李四", "text": "同意"}],
         "reactions": None, "path": "评论区"},
    ])
    run_migrations(db)

    rows = db.query_all("SELECT * FROM req_block WHERE req_id='REQ-2026-001' ORDER BY seq")
    # 块 ID 原样（anchor 零迁移）、comment 不入块表、seq 间隔 1000 按文档序
    assert [r["block_id"] for r in rows] == ["fs-title", "fs-h1", "fs-p1", "fs-li1", "fs-img1"]
    assert [r["seq"] for r in rows] == [1000, 2000, 3000, 4000, 5000]
    # attrs_json：排除 id/type/text（独立列）与派生 path/ordinal、comments（归属 req_comment）
    assert json.loads(rows[1]["attrs_json"]) == {"level": 2}
    assert json.loads(rows[3]["attrs_json"]) == {"marker": "•", "depth": 0, "parent": "fs-p1"}
    assert json.loads(rows[4]["attrs_json"]) == {"media": "m-abc"}

    # comment 块 → req_comment：thread 首条为根、后续 reply_to 链、external_id=comment-id
    cms = db.query_all("SELECT * FROM req_comment WHERE req_id='REQ-2026-001' ORDER BY id")
    assert len(cms) == 2
    assert cms[0]["origin"] == "feishu" and cms[0]["external_id"] == "cm-1"
    assert cms[0]["block_id"] == "fs-p1" and cms[0]["author"] == "张三" and cms[0]["text"] == "这里要改"
    assert cms[0]["resolved"] == 0 and cms[0]["created_at"] is None
    assert cms[1]["author"] == "李四" and cms[1]["reply_to"] == cms[0]["id"]

    # doc artifact 行保留归档（不删——回滚读面兜底）
    assert db.query_scalar("SELECT COUNT(*) FROM artifact WHERE type='doc' AND req_id='REQ-2026-001'") == 1

    run_migrations(db)  # 幂等：账本已登记，不再重复搬运
    assert len(db.query_all("SELECT * FROM req_block WHERE req_id='REQ-2026-001'")) == 5
    assert len(db.query_all("SELECT * FROM req_comment WHERE req_id='REQ-2026-001'")) == 2


def test_migration_skips_broken_doc_artifact(tmp_path: Path) -> None:
    db = _legacy_db(tmp_path)
    db.execute(
        "INSERT INTO artifact(req_id,type,markdown,content_json,version) VALUES('REQ-2026-001','doc','','{bad json',1)"
    )
    run_migrations(db)
    assert db.query_scalar("SELECT COUNT(*) FROM req_block") == 0
    assert db.query_scalar("SELECT COUNT(*) FROM req_comment") == 0


def test_clarify_origin_backfill(tmp_path: Path) -> None:
    db = _legacy_db(tmp_path)
    db.execute(
        "INSERT INTO clarify_item(req_id,seq,level,question,status) VALUES('REQ-2026-001','P1','yellow','q','pending')"
    )
    run_migrations(db)
    # 判列补列默认空串会被回填为 'skill'（否则「按来源合并」永远匹配不到存量行）
    assert db.query_scalar("SELECT origin FROM clarify_item WHERE req_id='REQ-2026-001'") == "skill"


def test_anchor_health_audit(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    db = _legacy_db(tmp_path)
    _insert_doc_artifact(db, [
        {"id": "fs-a", "type": "paragraph", "text": "a", "path": ""},
        {"id": "fs-b", "type": "paragraph", "text": "b", "path": ""},
    ])
    # 幽灵 anchor（不在块表）与空 anchor 各一——均应登记而非静默进关键路径
    db.execute(
        "INSERT INTO clarify_item(req_id,seq,level,question,status,anchor) VALUES('REQ-2026-001','P1','yellow','q','pending','GHOST-ID')"
    )
    db.execute(
        "INSERT INTO fr_point(req_id,id,name,anchor) VALUES('REQ-2026-001','FR-1','fr','')"
    )
    with caplog.at_level(logging.WARNING, logger="platform_sdlc.migrations"):
        run_migrations(db)
    joined = "\n".join(r.getMessage() for r in caplog.records)
    assert "anchor 健康度：clarify.P1 空/幻觉 anchor（req=REQ-2026-001 anchor='GHOST-ID'）" in joined
    assert "anchor 健康度汇总：2 条脏 anchor" in joined


# ---------- 运行期：重建行携带 origin（与账本回填同批的关键同期点） ----------

def test_push_artifacts_sets_skill_origin(tmp_path: Path) -> None:
    db = Database(tmp_path / "platform.db")
    run_migrations(db)
    repos = Repos.create(db)
    svc = RequirementService(db, repos)
    repos.requirement.insert(Requirement(
        id="REQ-2026-001", name="来源标记验证", source_type="text", source_text="# 原文",
        status="submitted", submitted_by="tester",
    ))
    svc.push_artifacts("REQ-2026-001", {
        "digest": {"md": "", "summary": "s", "roles": [], "features": [], "flows": [], "states": []},
        "audit": {"md": "", "red": 0, "yellow": 0, "blue": 0, "items": []},
        "items": [{"seq": "P1", "level": "yellow", "theme": "t", "question": "q"}],
    })
    items = repos.clarify_item.list_by_req("REQ-2026-001")
    assert len(items) == 1 and items[0].origin == "skill"


# ---------- 仓储面 ----------

def test_req_block_repo(tmp_path: Path) -> None:
    db = Database(tmp_path / "platform.db")
    run_migrations(db)
    repo = Repos.create(db).req_block

    assert repo.empty("REQ-X") is True
    assert repo.has_edit_trace("REQ-X") is False
    repo.replace_all("REQ-X", [ReqBlock(req_id="REQ-X", block_id="fs-1", seq=1000, type="paragraph", text="a")])
    assert repo.empty("REQ-X") is False
    assert repo.has_edit_trace("REQ-X") is False  # 飞书 ID + version=1 = 无编辑痕迹

    db.execute("UPDATE req_block SET version=2 WHERE block_id='fs-1'")
    assert repo.has_edit_trace("REQ-X") is True  # version>1 = 编辑痕迹

    # 软删的 blk- 前缀块不算痕迹（放弃变更的 insert 逆留下的软删行）；未软删的算
    repo.replace_all("REQ-X", [ReqBlock(req_id="REQ-X", block_id="blk-1", seq=1000, type="paragraph",
                                        deleted="2026-10-13")])
    assert repo.has_edit_trace("REQ-X") is False
    repo.replace_all("REQ-X", [ReqBlock(req_id="REQ-X", block_id="blk-1", seq=1000, type="paragraph")])
    assert repo.has_edit_trace("REQ-X") is True

    # 读面过滤软删行；但 empty 语义含软删行（曾有导入即非空）
    assert [b.block_id for b in repo.list_by_req("REQ-X")] == ["blk-1"]
    db.execute("UPDATE req_block SET deleted='x' WHERE block_id='blk-1'")
    assert repo.list_by_req("REQ-X") == []
    assert repo.empty("REQ-X") is False


def test_req_comment_repo(tmp_path: Path) -> None:
    db = Database(tmp_path / "platform.db")
    run_migrations(db)
    repo = Repos.create(db).req_comment

    root = repo.insert(ReqComment(
        id=0, req_id="REQ-X", block_id="fs-1", origin="feishu", external_id="cm-9",
        author="张三", text="首条",
    ))
    reply = repo.insert(ReqComment(
        id=0, req_id="REQ-X", block_id="", origin="platform", author="李四", text="回复", reply_to=root,
    ))
    assert isinstance(root, int) and isinstance(reply, int) and root > 0

    assert repo.has_platform("REQ-X") is True
    assert repo.delete_feishu("REQ-X") == 1  # 只删 feishu 行，平台评论不动
    left = repo.list_by_req("REQ-X")
    assert len(left) == 1 and left[0].origin == "platform" and left[0].reply_to == root
    assert repo.has_platform("REQ-X") is True


def test_clarify_item_roundtrip_origin(tmp_path: Path) -> None:
    db = Database(tmp_path / "platform.db")
    run_migrations(db)
    repos = Repos.create(db)
    repos.clarify_item.insert(ClarifyItem(
        id=0, req_id="REQ-X", seq="L01", level="yellow", question="评论升级项", origin="local",
    ))
    loaded = repos.clarify_item.list_by_req("REQ-X")
    assert len(loaded) == 1 and loaded[0].origin == "local"
