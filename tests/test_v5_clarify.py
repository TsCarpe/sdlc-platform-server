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

from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast, get_origin

import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from platform_sdlc.common import answer_text_of
from platform_sdlc.config import Settings
from platform_sdlc.db import Database
from platform_sdlc.main import create_app
from platform_sdlc.models import ClarifyItem
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
    assert by_seq["P01"]["options"] == [{"key": "A", "text": "选项A"}, {"key": "B", "text": "选项B"}]
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


def test_wire_carries_options_layer_answer_text() -> None:
    c = ClarifyItem(id=1, req_id="R", seq="P01", level="yellow", theme="t", question="q",
                    answer='{"kind":"option","optionKey":"A","optionText":"选项全文"}',
                    options_json='[{"key":"A","text":"选项全文"}]', module="教研", layer="3")
    out = wire(c)
    # 尾部追加四键（键序契约：既有 15 键不动，新增在尾）
    assert list(out)[-4:] == ["options", "module", "layer", "answerText"]
    assert out["options"] == [{"key": "A", "text": "选项全文"}]
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
