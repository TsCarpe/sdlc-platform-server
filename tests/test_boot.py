"""P0 验收：全新空库可启动、迁移幂等、/health 形状正确、错误响应键形正确。"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from platform_sdlc.config import Settings
from platform_sdlc.main import create_app


@pytest.fixture()
def client(tmp_path: Path, db_dsn: str) -> Iterator[TestClient]:
    settings = Settings(_env_file=None, db_dsn=db_dsn, media_dir=str(tmp_path / "data" / "media"))  # pyright: ignore[reportCallIssue]
    app = create_app(settings, cwd=tmp_path)
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


def test_health_shape(client: TestClient) -> None:
    r = client.get("/api/health")
    assert r.status_code == 200
    assert r.json() == {"ok": True, "app": "sdlc-platform", "version": "0.2.0"}


def test_migrations_idempotent(tmp_path: Path, db_dsn: str) -> None:
    settings = Settings(_env_file=None, db_dsn=db_dsn)  # pyright: ignore[reportCallIssue]
    app1 = create_app(settings, cwd=tmp_path)  # 首次：建库 + 全量迁移
    app1.state.db.close()
    app2 = create_app(settings, cwd=tmp_path)  # 二次：幂等无异常（表/索引已存在的 1050/1061 容忍实测）
    app2.state.db.close()


def test_unknown_path_error_shape(client: TestClient) -> None:
    r = client.get("/api/nope")
    assert r.status_code == 404
    assert r.json()["error"].startswith("资源不存在：")

    r2 = client.request("DELETE", "/api/health")
    assert r2.status_code == 405
    assert r2.json()["error"].startswith("请求方法不支持：")


def test_token_guard(tmp_path: Path, db_dsn: str) -> None:
    settings = Settings(_env_file=None, db_dsn=db_dsn, write_token="secret")  # pyright: ignore[reportCallIssue]
    app = create_app(settings, cwd=tmp_path)
    with TestClient(app) as c:
        denied = c.post("/api/anything", json={})
        assert denied.status_code == 401
        assert denied.json()["error"].startswith("未授权")  # 统一 {error} 形状（AUDIT T-10）
        ok = c.post("/api/anything", json={}, headers={"X-Token": "secret"})
        assert ok.status_code != 401  # 404（无此路由）但已过令牌
