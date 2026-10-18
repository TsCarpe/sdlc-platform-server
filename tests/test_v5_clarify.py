"""v1.6 验收（DESIGN-v5 §6.1/§6.3/§6.4 二期批 1）：澄清契约纯增键——

- answerText 单一解构出口（D15；判别四条件 D-E 收紧：分支负载空/残缺一律原文透传），
  answers 端点 + detail items（wire）双出口覆盖；
- set_answer 宽进结构化 JSON（零行为变化锁现状：原样落库不校验内部结构）；
- push_artifacts 宽松解析 options/module/layer（数字 layer 主线形态 D-J）+ flows[].pids
  一律数组（D-K）+ audit 条目 layer 归一（D-J 单点三用之一）；
- wire 尾部追加 options/module/layer/answerText 四键（键序契约）；
- answers/detail 路由保持 schema-free（键序由 _WIRE 手工序 + snapshot-keys 守门，
  挂 response_model 会破坏 NON_NULL 条件键视图——勿挂，测试锁定）。
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast, get_origin

import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from platform_sdlc.agent.prompts import _quality_quick_ref, sys_prompt
from platform_sdlc.common import answer_text_of, audit_item_view, normalize_layer
from platform_sdlc.config import Settings
from platform_sdlc.db import Database
from platform_sdlc.main import create_app
from platform_sdlc.models import ClarifyItem, ReqBlock, Requirement
from platform_sdlc.repositories import Repos
from platform_sdlc.schemas import wire


@pytest.fixture()
def env(tmp_path: Path) -> Iterator[tuple[TestClient, Database, Repos]]:
    settings = Settings(_env_file=None, db_path=str(tmp_path / "data" / "platform.db"),  # pyright: ignore[reportCallIssue]
                        media_dir=str(tmp_path / "data" / "media"))
    app = create_app(settings, cwd=tmp_path)
    client = TestClient(app, raise_server_exceptions=False)
    db = Database(tmp_path / "data" / "platform.db")
    repos = Repos.create(db)
    yield client, db, repos


def _mk_card(client: TestClient, name: str = "v5 澄清契约") -> str:
    created = client.post("/api/requirements", json={"name": name, "sourceType": "text",
                                                     "sourceText": "# 原文", "submittedBy": "tester"}).json()
    return str(created["id"])


# 表驱动（§6.3 判别四条件 + D-E 收紧）：输入 answer → 期望 answerText
_TABLE: list[tuple[str, str]] = [
    # 结构化 option：optionText ＋ extra 连接符定稿（补充：…）
    ('{"kind":"option","optionKey":"A","optionText":"选项全文","extra":"补充说明"}',
     "选项全文（补充：补充说明）"),
    ('{"kind":"option","optionKey":"B","optionText":"选项全文"}', "选项全文"),
    # 结构化 text
    ('{"kind":"text","text":"自由文本答复"}', "自由文本答复"),
    # D-E 收紧：分支负载空/残缺 → 一律原文透传（不产空串、不外溢 JSON）
    ('{"kind":"option","optionKey":"A"}', '{"kind":"option","optionKey":"A"}'),
    ('{"kind":"option","optionKey":"A","optionText":""}', '{"kind":"option","optionKey":"A","optionText":""}'),
    ('{"kind":"text","text":""}', '{"kind":"text","text":""}'),
    # review #12：分支负载纯空白 → 取值 .strip() 后判空，同残缺透传
    ('{"kind":"option","optionKey":"A","optionText":"   "}', '{"kind":"option","optionKey":"A","optionText":"   "}'),
    ('{"kind":"text","text":" \\t "}', '{"kind":"text","text":" \\t "}'),
    # kind 越界 / 无 kind 键 / 非 JSON → 原文
    ('{"kind":"other","text":"x"}', '{"kind":"other","text":"x"}'),
    ('{"optionText":"x"}', '{"optionText":"x"}'),
    ("纯文本答复", "纯文本答复"),
]


def test_answer_text_discrimination_table(env: tuple[TestClient, Database, Repos]) -> None:
    client, _, repos = env
    req_id = _mk_card(client)
    for i, (answer, _expected) in enumerate(_TABLE, start=1):
        repos.clarify_item.insert(ClarifyItem(
            id=0, req_id=req_id, seq=f"P{i:02d}", level="yellow", theme="t",
            question=f"q{i}", answer=answer, status="answered"))
    answers = {a["seq"]: a for a in client.get(f"/api/requirements/{req_id}/answers").json()}
    detail_items = {it["seq"]: it for it in client.get(f"/api/requirements/{req_id}").json()["items"]}
    for i, (answer, expected) in enumerate(_TABLE, start=1):
        seq = f"P{i:02d}"
        # 双出口同值：answers 端点（手写 dict 增键）+ detail items（wire 通道 property）
        assert answers[seq]["answerText"] == expected, f"answers 出口：{seq}"
        assert detail_items[seq]["answerText"] == expected, f"detail 出口：{seq}"
        assert answers[seq]["answer"] == answer  # 原始 answer 透传不重写
        assert answer_text_of(answer) == expected  # common 单点直测


def test_answer_text_none_and_blank() -> None:
    """review #13：入口收口 answer 基准——None/纯空白 → ""（原 None 透传返 None、空白原样
    返回），首尾空白非 JSON 文本以 strip 后值返回；空白负载结构化同残缺透传（透传 strip 后基准）。"""
    assert answer_text_of(None) == ""  # type: ignore[arg-type]
    assert answer_text_of("") == "" and answer_text_of("   ") == ""
    assert answer_text_of("  纯文本答复  ") == "纯文本答复"
    assert answer_text_of('  {"kind":"option","optionText":" "}  ') \
        == '{"kind":"option","optionText":" "}'


def test_normalize_layer_float_forms() -> None:
    """review #24：白名单外浮点整值归一——2.0/"2.0" → "2"（原归空丢主线浮点形态）；转换结果
    再过一次白名单（9.0 → "9" 不得漏越界拦截）；非整值/NaN/不可解析一律 ''。"""
    assert normalize_layer(2.0) == "2" and normalize_layer("2.0") == "2"
    assert normalize_layer("2.5") == ""
    assert normalize_layer(9.0) == "" and normalize_layer("9.0") == ""
    assert normalize_layer("nan") == "" and normalize_layer(float("nan")) == ""
    assert normalize_layer("abc") == ""


def test_set_answer_accepts_structured_json(env: tuple[TestClient, Database, Repos]) -> None:
    client, _, repos = env
    req_id = _mk_card(client)
    # push 后卡隐式推进 clarifying（PUT answers 的状态门禁），P01 随清单重建
    pushed = client.post(f"/api/requirements/{req_id}/artifacts", json={
        "digest": {"md": "", "summary": "s", "roles": [], "flows": [], "states": [], "features": []},
        "audit": {"md": "", "red": 0, "yellow": 0, "blue": 0, "items": []},
        "items": [{"seq": "P01", "level": "yellow", "theme": "t", "question": "选择题"}],
    })
    assert pushed.status_code == 200
    payload = '{"kind":"option","optionKey":"A","optionText":"选项全文","extra":"补充"}'
    r = client.put(f"/api/requirements/{req_id}/answers/P01",
                   json={"answer": payload, "status": "answered"})
    assert r.status_code == 200
    row = next(c for c in repos.clarify_item.list_by_req(req_id) if c.seq == "P01")
    # 宽进锁现状：answer 字符串原样落库（不校验内部结构），answered 态照常推进
    assert row.answer == payload and row.status == "answered"
    got = client.get(f"/api/requirements/{req_id}/answers").json()[0]
    assert got["answer"] == payload and got["answerText"] == "选项全文（补充：补充）"


def test_push_artifacts_parses_v5_optional_fields(env: tuple[TestClient, Database, Repos]) -> None:
    client, _, _ = env
    req_id = _mk_card(client)
    r = client.post(f"/api/requirements/{req_id}/artifacts", json={
        "digest": {"md": "", "summary": "s", "roles": [], "states": [], "features": [],
                   "flows": [{"role": "用户", "step": "提交", "next": "审核", "gap": False,
                              "anchor": "p1", "pids": ["P01", "P02"]}]},
        "audit": {"md": "", "red": 0, "yellow": 0, "blue": 0, "items": [
            {"id": "P01", "level": "yellow", "title": "t", "where": "w", "go": "", "group": "g",
             "quote": "", "anchor": "", "layer": 2},   # 数字主线形态 → "2"（D-J：先 str 化再判）
            {"id": "P02", "level": "yellow", "title": "t", "where": "w", "go": "", "group": "g",
             "quote": "", "anchor": "", "layer": "9"},  # 越界 → ''（不点亮就绪度伪桶）
            {"id": "P03", "level": "yellow", "title": "t", "where": "w", "go": "", "group": "g",
             "quote": "", "anchor": "", "layer": "0"},  # '0'（就绪度覆盖地图专用层）在澄清层归一为 ''
        ]},
        "items": [
            {"seq": "P01", "level": "yellow", "theme": "t", "question": "选择题",
             "options": [{"key": "A", "text": "选项A"}, {"key": "B", "text": "选项B"}],
             "module": "教研", "layer": 3},             # 数字 layer 主线用例（D-J）
            {"seq": "P02", "level": "yellow", "theme": "t", "question": "旧推送方行"},  # 宽松：三键全缺
        ],
    })
    assert r.status_code == 200
    detail = client.get(f"/api/requirements/{req_id}").json()
    by_seq = {it["seq"]: it for it in detail["items"]}
    assert by_seq["P01"]["options"] == [{"key": "A", "text": "选项A", "why": "", "recommended": False},
                                        {"key": "B", "text": "选项B", "why": "", "recommended": False}]
    assert by_seq["P01"]["module"] == "教研" and by_seq["P01"]["layer"] == "3"
    # 宽松分支：旧推送方（无 v1.6 三键）零扰动——options=[] / module=''/ layer=''
    assert by_seq["P02"]["options"] == [] and by_seq["P02"]["module"] == "" and by_seq["P02"]["layer"] == ""
    # flows[].pids 一律数组（D-K：字符串形态会以非空串绕过前端 (f.pids||[]).length 降级判定）
    flows = detail["digest"]["content"]["flows"]
    assert isinstance(flows[0]["pids"], list) and flows[0]["pids"] == ["P01", "P02"]
    # audit 条目 layer 归一（D-J 单点三用：audit items 同一 1-7 体系 §6.2）
    audit_items = {it["id"]: it for it in detail["audit"]["content"]["items"]}
    assert audit_items["P01"]["layer"] == "2"
    assert audit_items["P02"]["layer"] == "" and audit_items["P03"]["layer"] == ""


def test_push_options_element_whitelist(env: tuple[TestClient, Database, Repos]) -> None:
    """review #11：options 元素级四键白名单（写侧 push 归一落库 → 读侧 wire 同构）——私货键
    剥除、缺键补默认、非 obj 元素丢弃、空元素保留（宽进：空 key 前端匹配不上无副作用）；
    text 取值 strip（存储/answerText/前端三侧同构）。"""
    client, _, _ = env
    req_id = _mk_card(client)
    r = client.post(f"/api/requirements/{req_id}/artifacts", json={
        "digest": {"md": "", "summary": "s", "roles": [], "states": [], "features": [], "flows": []},
        "audit": {"md": "", "red": 0, "yellow": 0, "blue": 0, "items": []},
        "items": [
            {"seq": "P01", "level": "yellow", "theme": "t", "question": "选择题", "options": [
                {"key": "A", "text": " 选项A ", "why": "取舍理由", "recommended": True, "evil": "私货"},
                {"key": "B", "text": "选项B"},   # 存量两键形态：缺 why/recommended 补默认
                "垃圾",                          # 非 json obj 元素：丢弃
                {"key": "", "text": ""},         # 空元素：保留（宽进）
            ]},
        ],
    })
    assert r.status_code == 200
    detail = client.get(f"/api/requirements/{req_id}").json()
    it = next(t for t in detail["items"] if t["seq"] == "P01")
    assert it["options"] == [
        {"key": "A", "text": "选项A", "why": "取舍理由", "recommended": True},
        {"key": "B", "text": "选项B", "why": "", "recommended": False},
        {"key": "", "text": "", "why": "", "recommended": False},
    ]


def test_wire_carries_options_layer_answer_text() -> None:
    c = ClarifyItem(id=1, req_id="R", seq="P01", level="yellow", theme="t", question="q",
                    answer='{"kind":"option","optionKey":"A","optionText":"选项全文"}',
                    options_json='[{"key":"A","text":"选项全文"}]', module="教研", layer="3")
    out = wire(c)
    # 尾部追加四键（键序契约：既有 15 键不动，新增在尾）
    assert list(out)[-4:] == ["options", "module", "layer", "answerText"]
    assert out["options"] == [{"key": "A", "text": "选项全文", "why": "", "recommended": False}]
    assert out["module"] == "教研" and out["layer"] == "3"
    assert out["answerText"] == "选项全文"
    # 宽进降级：非法 options_json（非数组）→ []；空 answer → answerText=''
    bad = ClarifyItem(id=2, req_id="R", seq="P02", level="yellow", question="q",
                      options_json='{"not":"array"}')
    assert wire(bad)["options"] == [] and wire(bad)["answerText"] == ""


def _flatten_routes(routes: list[Any]) -> Iterator[Any]:
    """递归展开路由树（本项目 include_router 走 _IncludedRouter 懒挂载包装，端点路由
    不在 app.routes 顶层——经 original_router 下钻取平铺 APIRoute）。"""
    for route in routes:
        original = getattr(route, "original_router", None)
        if original is not None:
            yield from _flatten_routes(original.routes)
        else:
            yield route


def test_routes_stay_schema_free(env: tuple[TestClient, Database, Repos]) -> None:
    """answers/detail 的 response_model 保持透传形态（现状 = 返回注解 dict[str, Any] 自动生成
    的宽松模型，schema 无固定 properties，增键原样出参——answerText 落地即此形态验证）。锁死
    防回归：挂 _CamelModel 式具名模型会锁键集/键序，破坏 detail 的 NON_NULL 条件键视图
    （AUDIT D4-4）与 _WIRE 手工序键序契约。"""
    client, _, _ = env
    app = cast(FastAPI, client.app)
    hits: dict[str, APIRoute] = {}
    for route in _flatten_routes(list(app.routes)):
        if isinstance(route, APIRoute):
            if route.path == "/api/requirements/{req_id}/answers" and "GET" in (route.methods or set()):
                hits["answers"] = route
            if route.path == "/api/requirements/{req_id}" and "GET" in (route.methods or set()):
                hits["detail"] = route
    assert set(hits) == {"answers", "detail"}
    for name, route in hits.items():
        rm = route.response_model
        assert rm is not None, f"{name}：须保留返回注解（透传形态的来源）"
        origin = get_origin(rm) or rm  # list[dict[str,Any]] → list；dict[str,Any] → dict
        assert origin in (dict, list), \
            f"{name}：response_model 非透传形态——挂具名 pydantic 模型会锁键集/键序，破坏 NON_NULL 条件键视图"


# ---------- 批 2：变更链路 + 四通道消费（DESIGN-v5 §6.4/§7-4，A4/B6/D15） ----------

_OPTION_ANSWER = '{"kind":"option","optionKey":"A","optionText":"选项全文","extra":"补充说明"}'
_AUDIT_KEYS = {"id", "level", "title", "where", "go", "group", "quote", "anchor", "layer"}
# 跨仓契约 fixture（P1-2 hermetic）：仓内快照，CI 单仓 checkout 可跑；权威源与刷新方式见
# tests/fixtures/skills-contract/README.md
_CONTRACT_DIR = Path(__file__).resolve().parent / "fixtures" / "skills-contract"


def _seed_blocks(repos: Repos, req_id: str) -> None:
    """5 块基线：p1（变更 edit 目标锚，h1 章节）+ li2（h2 章节下——增量 sections=['h1'] 的区间外
    kept 锚：section_ids 是章节级集合，锚须在受影响章节之外而非仅块未编辑）。"""
    repos.req_block.replace_all(req_id, [
        ReqBlock(req_id=req_id, block_id="t1", seq=1000, type="title", text="标题"),
        ReqBlock(req_id=req_id, block_id="h1", seq=2000, type="heading", text="背景",
                 attrs_json='{"level":2}'),
        ReqBlock(req_id=req_id, block_id="p1", seq=3000, type="paragraph", text="一段说明"),
        ReqBlock(req_id=req_id, block_id="h2", seq=4000, type="heading", text="其他",
                 attrs_json='{"level":2}'),
        ReqBlock(req_id=req_id, block_id="li2", seq=5000, type="list-item", text="子项 a"),
    ])


def _push_and_publish(client: TestClient, req_id: str, *, p1_answer: str = "答 P1",
                      flows: list[dict[str, Any]] | None = None,
                      audit_items: list[dict[str, Any]] | None = None) -> None:
    """发布链：push（P1 锚 p1 / P2 锚 li2 带 v1.6 字段）→ 全处置 → publish。"""
    client.post(f"/api/requirements/{req_id}/artifacts", json={
        "digest": {"md": "", "summary": "s", "roles": [], "states": [], "features": [],
                   "flows": flows if flows is not None else []},
        "audit": {"md": "", "red": 0, "yellow": 0, "blue": 0,
                  "items": audit_items if audit_items is not None else []},
        "items": [
            {"seq": "P1", "level": "yellow", "theme": "t", "question": "q1", "anchor": "p1"},
            {"seq": "P2", "level": "yellow", "theme": "t", "question": "q2", "anchor": "li2",
             "options": [{"key": "A", "text": "甲"}], "module": "教研", "layer": "5"},
        ],
    })
    client.put(f"/api/requirements/{req_id}/answers/P1", json={"answer": p1_answer})
    client.put(f"/api/requirements/{req_id}/answers/P2", json={"answer": "答 P2"})
    client.post(f"/api/requirements/{req_id}/publish", json={"standardMd": "# 定稿"})


def _to_changed(client: TestClient, db: Database, req_id: str) -> int:
    """published → edit 变更单（p1 修订，h1 章节受影响）→ confirm → changed。返回变更单 id。"""
    cid = client.post(f"/api/requirements/{req_id}/changes", json={
        "title": "口径修订", "proposedBy": "张三", "blocks": [
            {"id": "t1", "type": "title", "text": "标题"},
            {"id": "h1", "type": "heading", "level": 2, "text": "背景"},
            {"id": "p1", "type": "paragraph", "text": "一段说明（修订）"},
            # h2/li2 原样透传（li2 在 h2 章节下 = 增量区间外，锚 li2 的行走 kept 路径）
            {"id": "h2", "type": "heading", "level": 2, "text": "其他"},
            {"id": "li2", "type": "list-item", "text": "子项 a"},
        ]}).json()["id"]
    db.execute("UPDATE artifact SET pushed_at='2026-10-13 00:00:00' WHERE req_id=? AND type='digest'",
               (req_id,))
    assert client.post(f"/api/requirements/{req_id}/changes/{cid}/confirm",
                       json={"confirmedBy": "李四"}).status_code == 200
    return int(cid)


def _v5_bundle(cid: int, *, flows: list[dict[str, Any]] | None = None,
               audit_items: list[dict[str, Any]] | None = None,
               clarify_items: list[dict[str, Any]] | None = None,
               features: list[dict[str, Any]] | None = None,
               states: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    b: dict[str, Any] = {
        "changeId": cid, "sections": ["h1"],
        "digestRows": {
            "features": features if features is not None else [],
            "flows": flows if flows is not None else [],
            "states": states if states is not None else [],
            "auditItems": audit_items if audit_items is not None else [],
            "frUpserts": [], "frRemoves": [],
        },
        "invalidate": {"digestAnchors": [], "auditIds": []},
        "clarify": {"items": clarify_items if clarify_items is not None else []},
        "summary": "增量后摘要", "riskTier": "A",
    }
    return b


def _approve_bundle(client: TestClient, req_id: str, bundle: dict[str, Any]) -> dict[str, Any]:
    r = client.put(f"/api/requirements/{req_id}/drafts/incremental_bundle",
                   json={"contentJson": json.dumps(bundle, ensure_ascii=False)})
    assert r.status_code == 200, r.text
    appr = client.post(f"/api/requirements/{req_id}/drafts/incremental_bundle/approve",
                       json={"by": "采纳人"})
    assert appr.status_code == 200, appr.text
    return appr.json()


def test_reopen_note_uses_answer_text(env: tuple[TestClient, Database, Repos]) -> None:
    """A4/D15：变更 confirm 的重开 note 拼解构文本（结构化作答不外溢 JSON 进 note/增量输入）；
    snapshot 存原值（放弃回滚按原文恢复）。"""
    client, db, repos = env
    req_id = _mk_card(client)
    _seed_blocks(repos, req_id)
    _push_and_publish(client, req_id, p1_answer=_OPTION_ANSWER)
    cid = _to_changed(client, db, req_id)
    answers = {a["seq"]: a for a in client.get(f"/api/requirements/{req_id}/answers").json()}
    # 重开 note = 解构文本；裸 JSON 不得进 note（D15：note 是增量合成输入与 PM 可见面）
    assert answers["P1"]["status"] == "pending"
    assert answers["P1"]["note"] == "因变更单#1 重开，原答复：选项全文（补充：补充说明）"
    assert '{"kind"' not in answers["P1"]["note"]
    # 快照存原值不切（§5.6 放弃回滚按原文恢复）
    row = db.query_one("SELECT snapshot_json FROM req_change WHERE id=?", (cid,))
    snapshot: dict[str, Any] = json.loads(str(row["snapshot_json"])) if row is not None else {}
    assert snapshot["reopen"][0]["answer"] == _OPTION_ANSWER


def test_incremental_merge_keeps_new_clarify_fields(env: tuple[TestClient, Database, Repos]) -> None:
    """B6：增量新澄清行 options/module/layer 宽松透传（layer 归一）——缺此三字段则增量新行
    相对全量推送永远降级。"""
    client, db, repos = env
    req_id = _mk_card(client)
    _seed_blocks(repos, req_id)
    _push_and_publish(client, req_id)
    cid = _to_changed(client, db, req_id)
    _approve_bundle(client, req_id, _v5_bundle(cid, clarify_items=[
        {"level": "yellow", "theme": "t", "question": "增量选择题", "anchor": "p1",
         "options": [{"key": "A", "text": "甲"}, {"key": "B", "text": "乙"}],
         "module": "教研", "layer": 3},  # 数字 layer 主线形态
        {"level": "yellow", "theme": "t", "question": "增量旧形态行", "anchor": "p1"},
    ]))
    items = {it["question"]: it for it in client.get(f"/api/requirements/{req_id}").json()["items"]}
    new1 = items["增量选择题"]
    assert new1["options"] == [{"key": "A", "text": "甲", "why": "", "recommended": False},
                               {"key": "B", "text": "乙", "why": "", "recommended": False}]
    assert new1["module"] == "教研" and new1["layer"] == "3"
    new2 = items["增量旧形态行"]
    assert new2["options"] == [] and new2["module"] == "" and new2["layer"] == ""


def test_incremental_merge_preserves_kept_row_fields(env: tuple[TestClient, Database, Repos]) -> None:
    """机器行重插往返：区间外 kept 行（skill 来源）经 delete_machine + insert_many 重建，
    v1.6 三字段与已答处置一并保留（r4 风险#7）。"""
    client, db, repos = env
    req_id = _mk_card(client)
    _seed_blocks(repos, req_id)
    _push_and_publish(client, req_id)  # P2（li2 区间外）带 options/module/layer + 已答
    cid = _to_changed(client, db, req_id)
    _approve_bundle(client, req_id, _v5_bundle(cid))
    answers = {a["seq"]: a for a in client.get(f"/api/requirements/{req_id}/answers").json()}
    assert answers["P2"]["answer"] == "答 P2" and answers["P2"]["status"] == "answered"
    assert answers["P2"]["origin"] == "skill"
    p2 = next(c for c in repos.clarify_item.list_by_req(req_id) if c.seq == "P2")
    # 落库串走 option_view 四键白名单（review #11；_json 序列化形态不作字面断言，解析比较）
    assert json.loads(str(p2.options_json)) == [{"key": "A", "text": "甲", "why": "", "recommended": False}]
    assert p2.module == "教研" and p2.layer == "5"


def test_incremental_audit_whitelist_unified(env: tuple[TestClient, Database, Repos]) -> None:
    """B6：增量合并 audit kept+new 全量过共享构造器——9 键白名单统一（私货剥除、layer 归一），
    与 push 侧 _audit_view 同一口径。断言面取 artifact content_json（合并层权威；detail 视图
    在其上叠加 relatedCases 等展示键，不在本用例范围）。"""
    client, db, repos = env
    req_id = _mk_card(client)
    _seed_blocks(repos, req_id)
    # 库里旧行（push 侧落库即 9 键归一）：li2 区间外 kept
    _push_and_publish(client, req_id, audit_items=[
        {"id": "P-OLD", "level": "yellow", "title": "旧条目", "where": "w", "go": "",
         "group": "g", "quote": "", "anchor": "li2"}])
    cid = _to_changed(client, db, req_id)
    _approve_bundle(client, req_id, _v5_bundle(cid, audit_items=[
        {"id": "A-1", "level": "yellow", "title": "新条目", "where": "w", "go": "确认",
         "anchor": "p1", "layer": 2, "foo": "私货"}]))
    raw = db.query_one("SELECT content_json FROM artifact WHERE req_id=? AND type='audit'", (req_id,))
    assert raw is not None
    items = {it["id"]: it for it in json.loads(str(raw["content_json"]))["items"]}
    assert set(items) == {"P-OLD", "A-1"}
    for iid, it in items.items():
        assert set(it) == _AUDIT_KEYS, f"{iid} 键集不是 9 键白名单：{set(it) ^ _AUDIT_KEYS}"
        assert "bar" not in it and "foo" not in it
    assert items["P-OLD"]["layer"] == ""       # 旧行无 layer → 构造器补默认（kept 也归一）
    assert items["A-1"]["layer"] == "2"        # new 行数字归一


def test_incremental_flows_whitelist_and_pids(env: tuple[TestClient, Database, Repos]) -> None:
    """D-K + D-I（三期白名单同构）：三键 new 行各过本键视图（flows pids 数组化/私货剥除）；
    kept flows（空 anchor global 行）合并前后逐字节不变（push 侧落库即归一 7 键，
    「永不改写」= 合并不重写历史数据）。features/states 的 new 行白名单归一见
    test_incremental_features_states_whitelist。"""
    client, db, repos = env
    req_id = _mk_card(client)
    _seed_blocks(repos, req_id)
    _push_and_publish(client, req_id, flows=[
        {"role": "系统", "step": "巡检", "anchor": ""},  # 空 anchor global 行（push 侧归一落库）
    ])
    cid = _to_changed(client, db, req_id)

    def _digest_flows() -> list[Any]:
        raw = db.query_one("SELECT content_json FROM artifact WHERE req_id=? AND type='digest'",
                           (req_id,))
        assert raw is not None
        return json.loads(str(raw["content_json"]))["flows"]

    kept_before = _digest_flows()
    # L 系 local 行（评论升级形态）：合并不删不重插、合并后真实存活——flows 引用它不悬空
    # （danglingPids 合法集须含 local，与 push 侧同口径——review #10 回归锁）
    db.execute(
        "INSERT INTO clarify_item(req_id,seq,level,theme,question,ctx,answer,answered_at,quote,"
        "anchor,status,note,origin,src_comment_id,options,module,layer) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (req_id, "L01", "yellow", "t", "评论升级问题", "", "", "", "", "", "pending", "",
         "local", None, "", "", ""))
    merge = _approve_bundle(client, req_id, _v5_bundle(
        cid,
        flows=[{"role": "用户", "step": "提交", "next": "审核", "anchor": "p1",
                "pids": ["L01"], "foo": "new 私货"}],
        features=[{"role": "用户", "m": "注册", "func": "邮箱注册", "cov": "全", "anchor": "p1"}],
        states=[{"obj": "工单", "from": "草稿", "on": "提交", "to": "审核", "anchor": "p1"}]))
    assert merge["danglingPids"] == 0  # L01=存活 local 引用合法（修复前 live_seqs 漏 local 即 1）
    flows = _digest_flows()
    assert len(flows) == 2
    assert flows[0] == kept_before[0]  # kept 行合并前后逐字节不变（永不改写）
    assert set(flows[0]) == {"role", "step", "next", "gap", "src", "anchor", "pids"}
    assert flows[1] == {"role": "用户", "step": "提交", "next": "审核", "gap": False,
                        "src": "", "anchor": "p1", "pids": ["L01"]}  # new 7 键白名单（foo 剥除）
    assert isinstance(flows[1]["pids"], list)
    # features/states new 行过本键视图：正当字段全保留（D-I 后与本键视图同构）
    content = client.get(f"/api/requirements/{req_id}").json()["digest"]["content"]
    assert content["features"][0]["m"] == "注册" and content["features"][0]["func"] == "邮箱注册"
    assert content["features"][0]["cov"] == "全"
    assert content["states"][0]["obj"] == "工单" and content["states"][0]["from"] == "草稿"


def test_prompt_channels_consume_answer_text() -> None:
    """四通道契约锁（r4 按分支断言）：standard_doc = answerText + 〔Pxx〕/〔Pxx·挂起〕；
    incremental = answerText 指令与其数据源步骤（list_clarify_items）共同在位——指令悬空即回退。"""
    root = Path(__file__).resolve().parents[1]
    runner_src = (root / "runner" / "runner.mjs").read_text(encoding="utf-8")
    # runner standard_doc 分支：answerText 消费 + 〔Pxx〕尾缀 + 挂起标注
    std_seg = runner_src.split("task.type === 'standard_doc'")[1].split("task.type === 'case_gen'")[0]
    assert "items[].answerText" in std_seg and "不要直接消费" in std_seg
    assert "〔Pxx〕" in std_seg and "〔Pxx·挂起〕" in std_seg
    # runner incremental 分支：输入侧 answerText 指令
    inc_seg = runner_src.split("task.type === 'incremental_check'")[1].split("task.type === 'standard_doc'")[0]
    assert "answerText" in inc_seg and "不要直接消费" in inc_seg
    # agent prompts：standard_doc = answerText + 〔Pxx〕；incremental = answerText 指令 + 数据源步骤
    card = Requirement(id="R", name="n")
    repo = Path("/repo")
    agent_std = sys_prompt("standard_doc", card, repo, [])
    assert "answerText" in agent_std and "不要直接消费" in agent_std
    assert "〔Pxx〕" in agent_std and "〔Pxx·挂起〕" in agent_std
    agent_inc = sys_prompt("incremental_check", card, repo, [])
    assert "answerText" in agent_inc and "list_clarify_items" in agent_inc  # 指令与数据源共同在位
    # agent 数据面 docstring：list_clarify_items 声明消费 answerText
    tools_src = (root / "src" / "platform_sdlc" / "agent" / "tools.py").read_text(encoding="utf-8")
    assert "answerText" in tools_src.split("async def list_clarify_items")[1].split("def impl")[0]
    # skills spec：answerText 消费规则在权威合成规范中。读仓内契约 fixture（P1-2 hermetic）——
    # 原实现直接读 ~/IdeaProjects/claude-sdlc-skills，CI 单仓 checkout 必 fail；live 对账另由
    # test_skills_contract_live_aligned_when_available 承担（缺失即 skip）
    spec = (_CONTRACT_DIR / "standard-doc-spec.md").read_text(encoding="utf-8")
    assert "answerText" in spec and "〔Pxx〕" in spec


def test_audit_view_layer_passthrough() -> None:
    """D-J 单点三用之直测：audit_item_view 的 layer 归一（数字主线/越界/'0'/缺省）。"""
    assert normalize_layer(2) == "2" and normalize_layer("7") == "7"
    assert normalize_layer("0") == "" and normalize_layer("9") == "" and normalize_layer("x") == ""
    assert normalize_layer("") == "" and normalize_layer(None) == ""
    base = {"id": "P1", "level": "yellow", "title": "t", "where": "w", "go": "",
            "group": "g", "quote": "", "anchor": "a"}
    assert audit_item_view({**base, "layer": 3})["layer"] == "3"
    assert audit_item_view(dict(base))["layer"] == ""           # 缺省补默认
    assert audit_item_view({**base, "layer": "0"})["layer"] == ""
    assert set(audit_item_view(base)) == _AUDIT_KEYS            # 9 键白名单


# ---------- 三期批 B1：runner ①② + agent 速查卡生产端（DESIGN-v5 §6.4/§8，v1.6 选择题化） ----------

def test_quality_prompt_carries_v16_fields() -> None:
    """生产端 ①② 契约锁：runner quality_check/incremental 分支与 agent 速查卡携带 v1.6 字段
    产出指令（options/module/layer/pids + 七层 + 契约版本引用）——静态断言防 prompt 回退。"""
    root = Path(__file__).resolve().parents[1]
    runner_src = (root / "runner" / "runner.mjs").read_text(encoding="utf-8")
    assert "六层" not in runner_src, "runner 仍含过时「六层」文案（dimensions 已七层）"
    qc_seg = runner_src.split("task.type === 'quality_check'")[1].split("task.type === 'incremental_check'")[0]
    for kw in ("七层", "payload 契约 v1.6", "组装 v1.6 payload", "options", "module", "layer", "pids"):
        assert kw in qc_seg, f"runner① quality_check 分支缺 v1.6 产出指令：{kw}"
    inc_seg = runner_src.split("task.type === 'incremental_check'")[1].split("task.type === 'standard_doc'")[0]
    for kw in ("options", "module", "layer", "pids", "存量已分配"):
        assert kw in inc_seg, f"runner② incremental 分支缺 v1.6 产出指令：{kw}"
    # agent 速查卡（对话被压缩后的权威）：payload 字段行与 pm-checklist 选项化提示同源
    ref = _quality_quick_ref()
    for kw in ("options", "module", "layer", "pids"):
        assert kw in ref, f"agent 速查卡缺 v1.6 字段：{kw}"
    assert "冲突/取舍类问题在问题格内换行列选项" in ref


_CONTRACT_KEYS = ("options", "module", "layer", "answerText", "danglingPids",
                  "incremental_bundle 草稿契约", "仅允许引用存量已分配 seq")


def test_skills_contract_structural_locked() -> None:
    """跨仓契约结构锁（P1-2 hermetic）：payload/incremental/标准文档三份规范的结构键必须齐备——
    runner/agent prompt 指示 LLM 读技能仓文档，结构键是执行侧真实依赖。
    不断言人类文档标题的版本字面量：版本标注属发布卫生，skills `5f3673c` 去掉 `v1.6` 标题后
    原断言变成环境探测器（CI 单仓 checkout 必红）。仓内 fixture 是契约快照，权威源与刷新见
    tests/fixtures/skills-contract/README.md；live 对账见下一用例（缺失即 skip）。"""
    version = json.loads((_CONTRACT_DIR / "contract-version.json").read_text(encoding="utf-8"))
    assert version["contract"] == "sdlc-sync-payload"
    schema_txt = (_CONTRACT_DIR / "payload-schema.md").read_text(encoding="utf-8")
    for key in _CONTRACT_KEYS:
        assert key in schema_txt, f"payload 契约结构键缺失：{key}"
    guide_txt = (_CONTRACT_DIR / "incremental-guide.md").read_text(encoding="utf-8")
    assert "仅允许引用存量已分配 seq" in guide_txt, "incremental-guide 缺 D-L 存量引用纪律"
    spec_txt = (_CONTRACT_DIR / "standard-doc-spec.md").read_text(encoding="utf-8")
    assert "answerText" in spec_txt and "〔Pxx〕" in spec_txt


def test_skills_contract_live_aligned_when_available() -> None:
    """live 契约对账（可选信号）：skills 仓检出存在时，用同一组结构键校验真实文档；
    仓库缺失（CI 单仓 checkout）即 skip——红灯只来自 fixture 结构锁，不依赖开发机布局。"""
    skills_root = Path(Settings().agent_skills_repo).expanduser()
    live = {
        "payload": skills_root / "skills" / "sdlc-sync" / "references" / "payload-schema.md",
        "guide": skills_root / "skills" / "sdlc-intent" / "references" / "incremental-guide.md",
        "spec": skills_root / "skills" / "sdlc-sync" / "references" / "standard-doc-spec.md",
    }
    if not all(p.exists() for p in live.values()):
        pytest.skip(f"skills 仓未检出（{skills_root}），跳过 live 契约对账")
    schema_txt = live["payload"].read_text(encoding="utf-8")
    for key in _CONTRACT_KEYS:
        assert key in schema_txt, f"live payload 契约结构键缺失：{key}"
    assert "仅允许引用存量已分配 seq" in live["guide"].read_text(encoding="utf-8")
    spec_txt = live["spec"].read_text(encoding="utf-8")
    assert "answerText" in spec_txt and "〔Pxx〕" in spec_txt


# ---------- 三期批 B2：D-I 白名单同构 + D-M curl + danglingPids（DESIGN-V5 §8 三期行） ----------

def test_incremental_features_states_whitelist(env: tuple[TestClient, Database, Repos]) -> None:
    """D-I：features/states 的 new 行过本键共享视图白名单归一（私货键剥除、缺省补齐）——
    与 flows（D-K）/audit（B6）三侧同构，裸 dict 透传会静默收纳未知键漂移；kept 行不改写。"""
    client, db, repos = env
    req_id = _mk_card(client)
    _seed_blocks(repos, req_id)
    _push_and_publish(client, req_id)
    cid = _to_changed(client, db, req_id)

    def _rows(key: str) -> list[Any]:
        raw = db.query_one("SELECT content_json FROM artifact WHERE req_id=? AND type='digest'",
                           (req_id,))
        assert raw is not None
        return json.loads(str(raw["content_json"]))[key]

    assert _rows("features") == [] and _rows("states") == []  # push 空基线（无 kept 行）
    merge = _approve_bundle(client, req_id, _v5_bundle(
        cid,
        features=[{"role": "用户", "m": "注册", "func": "邮箱注册", "cov": "全", "anchor": "p1",
                   "secret": "私货"}],
        states=[{"obj": "工单", "from": "草稿", "on": "提交", "to": "审核", "anchor": "p1",
                 "pids": ["不应有"]}]))
    feats, states = _rows("features"), _rows("states")
    assert feats == [{"role": "用户", "m": "注册", "func": "邮箱注册", "cov": "全",
                      "src": "", "anchor": "p1"}]  # 6 键白名单（secret 剥除、src 补默认）
    assert states == [{"obj": "工单", "from": "草稿", "on": "提交", "to": "审核", "gap": False,
                       "src": "", "anchor": "p1"}]  # 7 键白名单（states 无 pids 键——pids 剥除）
    # merge 出参 danglingPids（观察信号）：flows 引用不存在 seq 计数、不拒收（review #20a 收紧：
    # 本用例 bundle 不传 flows 恒 0 且键必在——恒真 or 断言改精确断言）
    assert merge["danglingPids"] == 0


def test_run_command_curl_allowed() -> None:
    """D-M：_CMD_ALLOW 含 curl（agent incremental 分支「run_command curl 直落 raw/」数据源死路
    修复）；LLM 可见面（_CMD_DOC 运行时构造）与白名单集合永远一致——断一致性而非断字面。"""
    from platform_sdlc.agent import tools
    assert "curl" in tools._CMD_ALLOW
    assert "curl" in tools._CMD_DOC
    assert "/".join(sorted(tools._CMD_ALLOW)) in tools._CMD_DOC  # 全集一致（增删命令自动跟随）
    # 装饰链：run_command 已是 FunctionTool（裸调用 function_tool 后重绑生效）
    assert any(getattr(t, "name", "") == "run_command" for t in tools.ALL_TOOLS)


def test_dangling_pids_reported_not_rejected(env: tuple[TestClient, Database, Repos]) -> None:
    """danglingPids 观测出参（P1 修订）：悬空 pids（⊄ 本 payload items seq ∪ 存量 L 系）计数正确、
    200 不拒收、落库原样透传（宽进语义锁定——清理走重推/生成端纪律，平台不改写）。"""
    client, _, repos = env   # db 未直用，env 形制与邻测一致
    req_id = _mk_card(client)
    _seed_blocks(repos, req_id)
    push = client.post(f"/api/requirements/{req_id}/artifacts", json={
        "digest": {"md": "", "summary": "s", "roles": [], "states": [], "features": [], "flows": [
            {"role": "用户", "step": "提交", "next": "?", "gap": True, "anchor": "p1",
             "pids": ["P1", "P99", "L01"]},   # P1=本 payload 合法；P99=悬空；L01=存量 L 系合法
        ]},
        "audit": {"md": "", "red": 0, "yellow": 0, "blue": 0, "items": []},
        "items": [{"seq": "P1", "level": "yellow", "theme": "t", "question": "q1", "anchor": "p1"}],
    })
    assert push.status_code == 200
    # P99 与 L01 均悬空 = 2：「存量 local 合法类」指卡上真实存在的 L 系行——本卡未升级过评论，
    # local_seqs 为空集，引用不存在的 L01 与 P99 同为悬空（合法集 = 本 payload seq ∪ 实存 local seq）
    assert push.json()["danglingPids"] == 2
    flows = client.get(f"/api/requirements/{req_id}").json()["digest"]["content"]["flows"]
    assert flows[0]["pids"] == ["P1", "P99", "L01"]  # 原样透传未剥除
