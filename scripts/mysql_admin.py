#!/usr/bin/env python3
"""MySQL 临时 schema 生命周期工具（verify-*.mjs / export-openapi.mjs 共用）。

用法：uv run python scripts/mysql_admin.py create-db|drop-db <库名>
DSN 来源：SDLC_DB_DSN（剥库名连 server 端执行，不依赖 DSN 里的库名存在）。
库名仅允许 [a-z0-9_]（防注入面：库名进 f-string DDL）。
"""
from __future__ import annotations

import os
import re
import sys

import pymysql

from platform_sdlc.db import parse_dsn


def main() -> int:
    if len(sys.argv) != 3 or sys.argv[1] not in ("create-db", "drop-db"):
        print(__doc__, file=sys.stderr)
        return 2
    cmd, name = sys.argv[1], sys.argv[2]
    if not re.fullmatch(r"[a-z0-9_]+", name):
        print(f"库名仅允许 [a-z0-9_]：{name!r}", file=sys.stderr)
        return 2
    dsn = os.environ.get("SDLC_DB_DSN", "")
    if not dsn:
        print("需要 SDLC_DB_DSN（mysql://user:pass@host:3306/db）", file=sys.stderr)
        return 2
    host, port, user, password, _ = parse_dsn(dsn)
    conn = pymysql.connect(host=host, port=port, user=user, password=password, autocommit=True)
    try:
        with conn.cursor() as cur:
            if cmd == "create-db":
                cur.execute(f"CREATE DATABASE `{name}` CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci")
            else:
                cur.execute(f"DROP DATABASE `{name}`")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
