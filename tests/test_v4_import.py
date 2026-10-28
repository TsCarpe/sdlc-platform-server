"""v4-3 验收：source_import 任务通道（DESIGN-v4 §3）——封存守卫、finish 完成回调落库（含
守恒终检 guard 对账）、noDraftTypes 两处代码分支、task_rules 出参、三处一致性守护纯函数。
固定向量用例与 claude-sdlc-skills parse_doc --self-test 同源（双仓算法一致性锁）。"""

from __future__ import annotations

import copy
import importlib.util
import json
from pathlib import Path
from typing import Any

import pytest

from platform_sdlc.agent.loop import AgentLoop
from platform_sdlc.common import is_json_obj
from platform_sdlc.config import Settings
from platform_sdlc.db import Database
from platform_sdlc.migrations import run_migrations
from platform_sdlc.models import MediaFile, Requirement
from platform_sdlc.repositories import Repos
from platform_sdlc.services import Services
from platform_sdlc.services.block import doc_text_stream, normalize_text_fingerprint

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
    "media": [{"id": "m-ghost", "token": "tok-ghost", "kind": "media", "name": "img.png",
               "mime": "image/png", "bytes": 10, "sha256": "0" * 64, "status": "ok"}],
}


def _with_guard(doc: dict[str, Any]) -> dict[str, Any]:
    """夹具挂合法 guard（conservation-v1）：textSha/mediaTokens 用平台同款算法现算（不硬编码）——
    media[].token 去重排序覆盖 guard.mediaTokens，指纹 = normalize(doc_text_stream(blocks))。"""
    d = copy.deepcopy(doc)
    media: list[Any] = d.get("media") or []
    tokens = sorted({str(m["token"]) for m in media if is_json_obj(m) and m["token"]})
    d["guard"] = {"algo": "conservation-v1", "source": "xml", "mediaTokens": tokens,
                  "textSha": normalize_text_fingerprint(doc_text_stream(d["blocks"]))}
    return d


def _seed_media(repos: Repos, req_id: str) -> None:
    """补齐 _DOC 引用的媒体行（m-ghost 已上传语义）——missingMedia=0 的前置。"""
    repos.media.insert(MediaFile(req_id=req_id, media_id="m-ghost", sha256="0" * 64,
                                 name="img.png", mime="image/png", bytes=10))


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


# ---------- finish 完成回调落库（守恒终检） ----------

def test_finish_import_success_with_guard(env: tuple[Database, Repos, Services, Path]) -> None:
    """合法 guard（对账通过）+ 媒体齐全 → done 落库；result 为落库统计（agent 摘要被覆盖）。"""
    _, repos, services, ws = env
    _mk_card(repos)
    _seed_media(repos, "REQ-2026-001")
    t = services.tasks.create_guarded("REQ-2026-001", "source_import", "", "")
    claimed = services.tasks.claim("test-runner")
    assert claimed is not None and claimed.id == t.id
    _mk_out(ws, t.id, _with_guard(_DOC))

    out = services.tasks.finish(t.id, True, {"result": "agent 摘要"})
    assert out.status == "done"
    stats = json.loads(out.result)
    assert stats == {"blocks": 3, "comments": 2, "missingMedia": 0}

    blocks = repos.req_block.list_by_req("REQ-2026-001")
    assert [b.block_id for b in blocks] == ["wk-h1", "wk-p1", "wk-img"]
    assert [b.seq for b in blocks] == [1000, 2000, 4000]  # comment 块占位 seq 3000（与迁移同语义）
    assert json.loads(blocks[0].attrs_json) == {"level": 1}  # path 派生不落
    cms = repos.req_comment.list_by_req("REQ-2026-001")
    assert len(cms) == 2 and cms[0].origin == "feishu" and cms[0].external_id == "cm-1"
    assert cms[1].reply_to == cms[0].id


def test_finish_ghost_media_blocked(env: tuple[Database, Repos, Services, Path]) -> None:
    """幽灵媒体（guard 对账全过、块引用媒体未上传）→ 阻断：任务 failed（reason=integrity-failed），
    块表不落（守恒门口径：missingMedia>0 拒绝，不再宽松计数落库）。"""
    _, repos, services, ws = env
    _mk_card(repos)
    t = services.tasks.create_guarded("REQ-2026-001", "source_import", "", "")
    services.tasks.claim("test-runner")
    _mk_out(ws, t.id, _with_guard(_DOC))  # 媒体行未补（m-ghost 不在库）

    out = services.tasks.finish(t.id, True, {})
    assert out.status == "failed"
    m = json.loads(out.result)
    assert m["reason"] == "integrity-failed"
    assert "媒体未上传" in m["error"]
    assert repos.req_block.empty("REQ-2026-001")  # 拒绝落库，块表保持空


def test_finish_rejects_missing_guard(env: tuple[Database, Repos, Services, Path]) -> None:
    """guard 缺失 → 409 拒（提示升级 parse_doc ≥0.4 重产），reason=integrity-failed。"""
    _, repos, services, ws = env
    _mk_card(repos)
    t = services.tasks.create_guarded("REQ-2026-001", "source_import", "", "")
    services.tasks.claim("test-runner")
    _mk_out(ws, t.id, _DOC)  # 无 guard（parse_doc <0.4 产物形状）

    out = services.tasks.finish(t.id, True, {})
    assert out.status == "failed"
    m = json.loads(out.result)
    assert m["reason"] == "integrity-failed"
    assert "缺守恒参照（guard）" in m["error"] and "parse_doc" in m["error"]


def test_finish_rejects_text_sha_mismatch(env: tuple[Database, Repos, Services, Path]) -> None:
    """textSha 不符（产物块文本与 guard 指纹不一致）→ 拒：防解析丢失/产物篡改。"""
    _, repos, services, ws = env
    _mk_card(repos)
    _seed_media(repos, "REQ-2026-001")
    t = services.tasks.create_guarded("REQ-2026-001", "source_import", "", "")
    services.tasks.claim("test-runner")
    doctored = _with_guard(_DOC)
    doctored["blocks"][1]["text"] = "被改写的段落"  # guard 按原文算，产物文本漂移
    _mk_out(ws, t.id, doctored)

    out = services.tasks.finish(t.id, True, {})
    assert out.status == "failed"
    m = json.loads(out.result)
    assert m["reason"] == "integrity-failed"
    assert "textSha 不一致" in m["error"]


def test_finish_rejects_media_status_failed(env: tuple[Database, Repos, Services, Path]) -> None:
    """media status=failed（exit 1 带病回传）→ 拒：平台口径比 agent 侧多查 status 全 ok。"""
    _, repos, services, ws = env
    _mk_card(repos)
    t = services.tasks.create_guarded("REQ-2026-001", "source_import", "", "")
    services.tasks.claim("test-runner")
    doctored = copy.deepcopy(_DOC)
    doctored["media"][0]["status"] = "failed: lark-cli not found"
    _mk_out(ws, t.id, _with_guard(doctored))

    out = services.tasks.finish(t.id, True, {})
    assert out.status == "failed"
    m = json.loads(out.result)
    assert m["reason"] == "integrity-failed"
    assert "status 非 ok" in m["error"]


def test_finish_allows_skipped_size_exceeded(env: tuple[Database, Repos, Services, Path]) -> None:
    """media status=skipped:size-exceeded（大附件豁免，DESIGN-飞书接入Python化 §5）→ 放行：
    status 终检与 missingMedia 面同步豁免（豁免媒体不上传不阻断，块的 media 引用不视为缺失）。"""
    _, repos, services, ws = env
    _mk_card(repos)
    _seed_media(repos, "REQ-2026-001")
    t = services.tasks.create_guarded("REQ-2026-001", "source_import", "", "")
    services.tasks.claim("test-runner")
    doctored = copy.deepcopy(_DOC)
    doctored["media"][0]["status"] = "skipped:size-exceeded"
    _mk_out(ws, t.id, _with_guard(doctored))

    out = services.tasks.finish(t.id, True, {})
    assert out.status == "done"
    assert json.loads(out.result)["missingMedia"] == 0


def test_finish_allows_markdown_fallback(env: tuple[Database, Repos, Services, Path]) -> None:
    """markdown-fallback（--md 回退）无原文可对账 → 仅结构校验：结构合法 + 媒体齐全即放行。"""
    _, repos, services, ws = env
    _mk_card(repos)
    _seed_media(repos, "REQ-2026-001")
    t = services.tasks.create_guarded("REQ-2026-001", "source_import", "", "")
    services.tasks.claim("test-runner")
    _mk_out(ws, t.id, {**copy.deepcopy(_DOC), "guard": {"algo": "markdown-fallback"}})

    out = services.tasks.finish(t.id, True, {})
    assert out.status == "done"
    assert json.loads(out.result) == {"blocks": 3, "comments": 2, "missingMedia": 0}


def test_finish_rejects_non_object_block(env: tuple[Database, Repos, Services, Path]) -> None:
    """blocks 混入非对象元素 → 拒（给 index 与类型；不再静默跳过）。"""
    _, repos, services, ws = env
    _mk_card(repos)
    _seed_media(repos, "REQ-2026-001")
    t = services.tasks.create_guarded("REQ-2026-001", "source_import", "", "")
    services.tasks.claim("test-runner")
    doctored = _with_guard(_DOC)
    doctored["blocks"].insert(2, "not-a-block")
    _mk_out(ws, t.id, doctored)

    out = services.tasks.finish(t.id, True, {})
    assert out.status == "failed"
    m = json.loads(out.result)
    assert m["reason"] == "integrity-failed"
    assert "blocks[2] 不是对象" in m["error"] and "str" in m["error"]


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
    """幂等重跑（§3）：无平台数据的卡重导 = 全量替换块 + feishu 评论，不叠加（带合法 guard、
    媒体齐全的干净夹具，两轮 finish 仍 done）；
    （有平台评论的卡重导被封存守卫拒——见 test_import_seal_guards，两测试互补覆盖 §2.4/§3 边界。）"""
    _, repos, services, ws = env
    _mk_card(repos, source_type="feishu")
    _seed_media(repos, "REQ-2026-001")
    for i in (1, 2):
        t = services.tasks.create_guarded("REQ-2026-001", "source_import", f"round-{i}", "")
        services.tasks.claim("test-runner")
        _mk_out(ws, t.id, _with_guard(_DOC))
        out = services.tasks.finish(t.id, True, {})
        assert out.status == "done"
    blocks = repos.req_block.list_by_req("REQ-2026-001")
    assert len(blocks) == 3  # 替换不叠加
    cms = repos.req_comment.list_by_req("REQ-2026-001")
    assert len(cms) == 2 and all(c.origin == "feishu" for c in cms)  # feishu 行替换不叠加
    # 注：平台评论回环（导出体粘回）不在此通道——封存守卫先拒（有 platform 评论的飞书源卡
    # 全量重导 409，见 _guard_import_sealed）；回环防御在 reconcile 通道
    # （test_v4_comments.py::test_reconcile_skips_platform_comment_echo）


# ---------- approve 剥 doc：守恒门只守外部导入（source_import / sdlc-sync） ----------

def test_approve_strips_legacy_doc_without_guard(env: tuple[Database, Repos, Services, Path]) -> None:
    """存量质检草稿带 doc（平台导出回环、无 guard）→ 采纳成功：doc 在 approve 链被剥除
    （不进守恒终检、不落 artifact），digest/audit 正常落库，存量 doc artifact 不被覆盖。"""
    _, repos, services, _ = env
    _mk_card(repos)
    repos.artifact.upsert("REQ-2026-001", "doc", "",
                          '{"blocks":[{"id":"legacy","type":"paragraph","text":"存量"}],"media":[]}', "2026-01-01")
    payload: dict[str, Any] = {
        "digest": {"md": "", "summary": "s", "roles": [], "features": [], "flows": [], "states": [], "fr_points": []},
        "audit": {"md": "", "red": 0, "yellow": 0, "blue": 0, "items": []},
        "items": [{"seq": "P1", "level": "yellow", "theme": "t", "question": "q"}],
        "doc": {"blocks": [{"id": "x", "type": "paragraph", "text": "回环"}], "media": []},  # 无 guard（平台导出形状）
    }
    services.tasks.put_draft("REQ-2026-001", "digest_bundle",
                             {"contentJson": json.dumps(payload, ensure_ascii=False)})
    out = services.tasks.approve("REQ-2026-001", "digest_bundle", "tester")
    assert out["ok"] is True and out["docUpdated"] is False and out["draftApproved"] is True
    assert repos.artifact.find("REQ-2026-001", "digest") is not None  # digest/audit 正常落库
    assert repos.artifact.find("REQ-2026-001", "audit") is not None
    kept_row = repos.artifact.find("REQ-2026-001", "doc")
    assert kept_row is not None
    kept = json.loads(kept_row.content_json or "{}")
    assert kept["blocks"][0]["id"] == "legacy"  # 存量 doc 未被草稿回环 doc 覆盖


# ---------- 守恒算法固定向量（与 claude-sdlc-skills parse_doc --self-test 同源） ----------

def test_conservation_vector() -> None:
    """双仓算法一致性锁：同一段 blocks 在平台侧与 skills 侧 --self-test 必须得出同一 textSha
    （期望值 = claude-sdlc-skills parse_doc --self-test 的 FIXED_VECTOR_SHA 常量）。"""
    blocks = [{"type": "paragraph", "text": "活动链接：必填"},
              {"type": "list-item", "text": "生效范围"},
              {"type": "image", "caption": "PC端原型图"}]
    assert normalize_text_fingerprint(doc_text_stream(blocks)) == \
        "61be48ea05d60cc30664afae48879306f5504495306cadaf48ec0d324c4716d9"


# ---------- noDraftTypes 与 self_check 跳过 ----------

def test_loop_self_check_skips_no_draft_types(env: tuple[Database, Repos, Services, Path]) -> None:
    _, repos, services, _ = env
    settings = Settings(_env_file=None)  # pyright: ignore[reportCallIssue]
    loop = AgentLoop(settings, repos, services)
    _mk_card(repos)
    t = services.tasks.create_guarded("REQ-2026-001", "source_import", "", "")
    assert loop.self_check(t) is None  # 无草稿面：跳过（不查不存在的草稿）


# ---------- 两处一致性守护纯函数 ----------

def test_check_consistency_detects_drift() -> None:
    check_consistency = _ctt.check_consistency

    base = ({"quality_check", "source_import"}, {"quality_check", "source_import"},
            {"quality_check": "digest_bundle"}, {"source_import"})
    assert check_consistency(*base) == []  # 一致通过

    bad = list(base)
    bad[3] = set()  # ② task_rules 漏 source_import（NO_DRAFT_TYPES 漏登记）
    assert any("①②" in p for p in check_consistency(*bad))

    bad = list(base)
    bad[1] = {"quality_check"}  # goal_prompt 漏 source_import 分支（会静默落 case_gen 兜底句）
    assert any("goal_prompt" in p for p in check_consistency(*bad))


def test_check_task_templates_main_green() -> None:
    """真文件全链（CI 第五道门同款入口）。"""
    assert _ctt.main() == 0


# ---------- v4-4：L2 建卡自动排 + 质检启动守卫 + 存量补排 ----------

def test_on_created_auto_once(env: tuple[Database, Repos, Services, Path]) -> None:
    _, repos, services, _ = env
    _mk_card(repos)
    orch = services.orchestration
    orch.on_created("REQ-2026-001")
    orch.on_created("REQ-2026-001")  # 二次触发不再建（只自动一次）
    rows = repos.task.list("REQ-2026-001", None, 100)
    imports = [t for t in rows if t.type == "source_import"]
    assert len(imports) == 1 and imports[0].created_by == "auto:card-created"


def test_on_created_failure_does_not_raise(env: tuple[Database, Repos, Services, Path]) -> None:
    _, repos, services, _ = env
    # 无卡场景：create_guarded 会 404 → 钩子吞掉（建卡主操作不连累）
    services.orchestration.on_created("REQ-NONE-404")
    _mk_card(repos)  # 后续建卡照常


def test_claim_quality_check_guard_blocks_empty(env: tuple[Database, Repos, Services, Path]) -> None:
    _, repos, services, _ = env
    _mk_card(repos)
    services.tasks.create_guarded("REQ-2026-001", "quality_check", "", "")
    # 块表空：领取即守卫拦截（返回 None，任务被置 failed 带引导文案）
    assert services.tasks.claim("runner-x") is None
    rows = repos.task.list("REQ-2026-001", "failed", 100)
    t = next(x for x in rows if x.type == "quality_check")
    m = json.loads(t.result)
    assert m["reason"] == "blocks-empty" and "先完成 source_import" in m["error"]

    # 块表就位：同构重试路径放行（retry → queued → claim 领到 running）
    services.tasks.retry(t.id)
    repos.req_block.replace_all("REQ-2026-001", [
        __import__("platform_sdlc.models", fromlist=["ReqBlock"]).ReqBlock(
            req_id="REQ-2026-001", block_id="x-1", seq=1000, type="paragraph", text="a")])
    got = services.tasks.claim("runner-x")
    assert got is not None and got.type == "quality_check" and got.status == "running"


def test_backfill_text_cards_import(tmp_path: Path) -> None:
    from platform_sdlc.migrations import _backfill_text_cards_import
    from platform_sdlc.migrations import run_migrations as rm

    db2 = Database(tmp_path / "legacy2.db")
    rm(db2)  # 先建全 schema（账本全跑完，本库无数据）
    db2.execute("INSERT INTO requirement(id,name,source_type,status) VALUES('REQ-2026-100','已有导入任务','text','submitted')")
    db2.execute("INSERT INTO requirement(id,name,source_type,status) VALUES('REQ-2026-101','飞书卡','feishu','submitted')")
    db2.execute("INSERT INTO requirement(id,name,source_type,status,deleted_at) VALUES('REQ-2026-102','已删','text','submitted','x')")
    db2.execute("INSERT INTO requirement(id,name,source_type,status) VALUES('REQ-2026-103','应补排','text','submitted')")
    db2.execute("INSERT INTO task(req_id,type,status) VALUES('REQ-2026-100','source_import','done')")
    _backfill_text_cards_import(db2)
    rows = db2.query_all(
        "SELECT req_id FROM task WHERE type='source_import' AND created_by='migration:v4' ORDER BY req_id")
    # 只补排「text + 存活 + 无块表 + 无导入任务」的卡；已有任务卡跳过（幂等）、已删卡不补、
    # feishu 卡不补（对账重导走三期变更单）
    assert [r["req_id"] for r in rows] == ["REQ-2026-103"]
