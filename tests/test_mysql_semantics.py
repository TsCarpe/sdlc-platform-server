"""MySQL 适配层语义冒烟（M2 首验三连，EXEC-PLAN-K8s 阶段 2）。

对齐 SQLite 时期的三项隐式语义——适配层漏配时的受害者回归网：
1. autocommit：非 tx 单条写对第二连接立即可见（PyMySQL 默认 autocommit=False 会静默丢写）；
2. DATETIME 出口归一：建卡出参 createdAt 为 Times.FMT 字符串（无 ISO 的 T 分隔）；
3. CLIENT.FOUND_ROWS：同值重写 rowcount=matched（默认 changed 会让 set_disposition 误报 404）。
"""

from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path

from fastapi.testclient import TestClient

from platform_sdlc.config import Settings
from platform_sdlc.db import Database
from platform_sdlc.main import create_app
from platform_sdlc.models import ClarifyItem, Requirement
from platform_sdlc.repositories import Repos
from platform_sdlc.services import Services

_F = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")


def test_autocommit_write_visible_to_second_connection(make_database: Callable[..., Database]) -> None:
    a = make_database()
    b = make_database()  # 第二连接（不同池实例，同库）
    a.execute("CREATE TABLE IF NOT EXISTS t_ac (id INT PRIMARY KEY, v VARCHAR(8) DEFAULT '')")
    a.execute("INSERT INTO t_ac(id, v) VALUES(1, 'x')")  # 非 tx 单条写：无显式 commit
    assert b.query_scalar("SELECT v FROM t_ac WHERE id=1") == "x"  # 立即可见（漏 autocommit=True 即红）


def test_datetime_out_col_is_times_fmt(tmp_path: Path, db_dsn: str) -> None:
    settings = Settings(_env_file=None, db_dsn=db_dsn, media_dir=str(tmp_path / "data" / "media"))  # pyright: ignore[reportCallIssue]
    app = create_app(settings, cwd=tmp_path)
    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            r = client.post("/api/requirements", json={"name": "时间格式冒烟", "sourceType": "text", "sourceText": "x"})
            assert r.status_code == 200
            detail = client.get(f"/api/requirements/{r.json()['id']}")
            assert detail.status_code == 200
            at = detail.json()["card"]["createdAt"]
            assert _F.match(str(at)), f"createdAt 应为 Times.FMT（无 T 无偏移）：{at}"
    finally:
        app.state.db.close()


def test_found_rows_same_value_rewrite_counts(make_database: Callable[..., Database], tmp_path: Path) -> None:
    db = make_database()
    from platform_sdlc.migrations import run_migrations

    run_migrations(db)
    repos = Repos.create(db)
    services = Services.create(db, repos, tmp_path / "media", tmp_path)
    repos.requirement.insert(Requirement(id="REQ-T", name="FOUND_ROWS", source_type="text", status="clarifying"))
    repos.clarify_item.insert(ClarifyItem(id=0, req_id="REQ-T", seq="P01", level="yellow", question="q"))
    services.requirements.set_answer("REQ-T", "P01", {"answer": "同一答案"})
    services.requirements.set_answer("REQ-T", "P01", {"answer": "同一答案"})  # 同值重写：matched=1 不误报 404
    items = repos.clarify_item.list_by_req("REQ-T")
    assert len(items) == 1 and items[0].answer == "同一答案"
