"""pytest 全局：models.TestCase 名字命中 Test* 收集规则，显式豁免（保持产码零测试框架痕迹）。

MySQL 测试基建（EXEC-PLAN-K8s 阶段 2）：M1 批定义、暂无消费方（现有测试照跑 SQLite）；
M2 切换批起全部测试改走 db_dsn。
- mysql_dsn（session 级）：SDLC_TEST_MYSQL_DSN 直连优先（CI service / 本地 compose 复用同一实例），
  否则懒启动 testcontainers 容器（镜像钉 8.0.40——滚动 tag mysql:8.0 在 8.0.29 前无 arm64 manifest）。
- db_dsn（函数级）：每测试独立 schema（CREATE DATABASE → yield → DROP），库名全小写
  （规避 macOS/linux lower_case_table_names 差异）。不支持 xdist（多进程 teardown 竞态）。
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from urllib.parse import unquote, urlparse

import pymysql
import pytest

from platform_sdlc import models

models.TestCase.__test__ = False  # type: ignore[attr-defined]

_MYSQL_IMAGE = "mysql:8.0.40"


def _parse_dsn(dsn: str) -> tuple[str, int, str, str]:
    """解析 mysql://user:pass@host:port/db → (host, port, user, password)；密码做百分号解码。"""
    u = urlparse(dsn)
    if u.scheme != "mysql" or not u.hostname or not u.username:
        raise ValueError(f"MySQL DSN 形如 mysql://user:pass@host:3306/db，收到: {dsn!r}")
    return u.hostname, u.port or 3306, u.username, unquote(u.password or "")


def _admin_execute(server_dsn: str, ddl: str) -> None:
    """在 server 级执行建库/删库 DDL（不选 database——目标库可能尚不存在）。"""
    host, port, user, password = _parse_dsn(server_dsn)
    conn = pymysql.connect(host=host, port=port, user=user, password=password, autocommit=True)
    try:
        with conn.cursor() as cur:
            cur.execute(ddl)
    finally:
        conn.close()


@pytest.fixture(scope="session")
def mysql_dsn() -> Iterator[str]:
    """session 级服务端 DSN（指向实例，库名部分仅占位——消费方用 db_dsn 建独立库）。"""
    direct = os.environ.get("SDLC_TEST_MYSQL_DSN", "")
    if direct:
        yield direct
        return
    from testcontainers.mysql import MySqlContainer

    with MySqlContainer(_MYSQL_IMAGE, username="root", password="test", dbname="test") as container:
        host = container.get_container_host_ip()
        port = container.get_exposed_port(3306)
        yield f"mysql://root:test@{host}:{port}/test"


@pytest.fixture()
def db_dsn(mysql_dsn: str) -> Iterator[str]:
    """函数级独立 schema：M2 起每测试的 Database / Settings 都从这里取 DSN。"""
    name = f"sdlc_{uuid.uuid4().hex[:8]}"
    _admin_execute(mysql_dsn, f"CREATE DATABASE `{name}` CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci")
    base = mysql_dsn.rsplit("/", 1)[0]
    try:
        yield f"{base}/{name}"
    finally:
        _admin_execute(mysql_dsn, f"DROP DATABASE `{name}`")
