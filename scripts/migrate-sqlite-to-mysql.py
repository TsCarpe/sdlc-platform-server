#!/usr/bin/env python3
"""一次性导入：本机 SQLite 开发库 → MySQL（M4，EXEC-PLAN-K8s 阶段 2）。

两阶段：
1. dry-run 硬门（任一不过即非零退出，不写目标库）：
   - dup iter：存活需求同迭代多卡（唯一索引守护会跳过，语义应清完再导——fail-fast）
   - VARCHAR 超长：长度取目标库 information_schema.character_maximum_length（与真实 schema 同源）
   - 纯时间列脏值：非 Times.FMT 且非 ''（DATETIME 拒收非法串）
2. 导入：sqlite3 .backup 快照 → 18 表按名对齐逐批 INSERT（时间列 ''→None；
   显式插自增列后 MySQL 自动把计数器推到 max+1）→ schema_version 账本整表复制
   （**关键**：不复制则 migrate_doc_artifact_to_blocks 等一次性迁移重跑——评论无幂等键会重复插入）。

用法：SDLC_DB_DSN=mysql://... uv run python scripts/migrate-sqlite-to-mysql.py \
        [--sqlite data/platform.db] [--recreate]
  --recreate：先 DROP DATABASE 再走 run_migrations 全新建表（默认要求目标库为空库或 --recreate）。
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
import tempfile
from pathlib import Path

import pymysql
import pymysql.cursors

from platform_sdlc.common import Times
from platform_sdlc.db import parse_dsn

ROOT = Path(__file__).resolve().parent.parent

# 逻辑依赖序（无外键，按可读性定序；schema_version 最后复制账本）
_TABLES = [
    "requirement", "iteration", "clarify_item", "artifact", "fr_point", "media",
    "test_case", "case_comment", "task", "draft_artifact", "seq_case_no",
    "gate_round", "gate_issue", "req_block", "req_comment", "req_change", "req_change_item",
    "schema_version",
]

# DATETIME 化的纯时间列（''→None；双态列 deleted_at/req_block.deleted 是 VARCHAR 原样导）
_DATETIME_COLS: dict[str, set[str]] = {
    "requirement": {"created_at", "synced_at"},
    "clarify_item": {"answered_at"},
    "artifact": {"pushed_at"},
    "media": {"created_at"},
    "test_case": {"created_at", "updated_at"},
    "case_comment": {"created_at"},
    "task": {"created_at", "updated_at"},
    "draft_artifact": {"updated_at"},
    "schema_version": {"applied_at"},
    "iteration": {"created_at"},
    "gate_round": {"released_at", "opened_at"},
    "gate_issue": {"verdict_at"},
    "req_block": {"updated_at"},
    "req_comment": {"created_at"},
    "req_change": {"aborted_at", "merged_at", "created_at", "confirmed_at"},
}

_F = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")


def _fail(msg: str) -> None:
    print(f"[dry-run FAIL] {msg}", file=sys.stderr)
    sys.exit(1)


def _sqlite_snapshot(src: Path) -> sqlite3.Connection:
    """.backup 快照（绝不直接读 WAL 库本体）；返回快照连接（调用方 close）。"""
    tmp = Path(tempfile.mkdtemp(prefix="sdlc-mig-")) / "snapshot.db"
    src_conn = sqlite3.connect(str(src))
    snap = sqlite3.connect(str(tmp))
    with snap:
        src_conn.backup(snap)
    src_conn.close()
    snap.row_factory = sqlite3.Row
    return snap


def _mysql_conn(dsn: str, database: str | None = None) -> pymysql.connections.Connection:
    host, port, user, password, db = parse_dsn(dsn)
    return pymysql.connect(host=host, port=port, user=user, password=password,
                           database=database or db, charset="utf8mb4", autocommit=True,
                           cursorclass=pymysql.cursors.DictCursor)


def _varchar_limits(conn: pymysql.connections.Connection) -> dict[tuple[str, str], int]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT table_name AS t, column_name AS c, character_maximum_length AS n "
            "FROM information_schema.columns WHERE table_schema=DATABASE() "
            "AND data_type IN ('varchar','varbinary')")
        # pyright: pymysql 泛型默认按普通 Cursor 标注，运行时为 DictCursor——键访问行尾豁免
        return {(str(r["t"]), str(r["c"])): int(r["n"]) for r in cur.fetchall() if r["n"] is not None}  # pyright: ignore[reportArgumentType, reportUnknownArgumentType, reportCallIssue]


def _columns(snap: sqlite3.Connection, table: str) -> list[str]:
    return [str(r["name"]) for r in snap.execute(f"PRAGMA table_info({table})").fetchall()]


def dry_run(snap: sqlite3.Connection, limits: dict[tuple[str, str], int]) -> None:
    print("== dry-run 硬门 ==")
    # 1) dup iter
    dup = snap.execute(
        "SELECT iter, COUNT(*) AS n FROM requirement WHERE iter!='' AND deleted_at='' "
        "GROUP BY iter HAVING COUNT(*)>1 ORDER BY n DESC").fetchall()
    if dup:
        _fail("存活需求存在同迭代多卡（先在 SQLite 侧清干净再导，守护索引语义=干净库才有唯一索引）：\n"
              + "\n".join(f"  iter={r['iter']} × {r['n']}" for r in dup))
    print("  dup iter：无 ✅")

    # 2) VARCHAR 超长 + 3) 时间列脏值
    overlong = 0
    bad_times: list[str] = []
    for table in _TABLES:
        cols = _columns(snap, table)
        if not cols:
            _fail(f"SQLite 快照缺表 {table}（源库不完整？）")
        vc = [c for c in cols if (table, c) in limits]
        tc = [c for c in cols if c in _DATETIME_COLS.get(table, set())]
        if not vc and not tc:
            continue
        for r in snap.execute(f"SELECT {', '.join(vc + tc)} FROM {table}"):  # noqa: S608（列名白名单）
            for c in vc:
                v = r[c]
                if isinstance(v, str) and len(v) > limits[(table, c)]:
                    overlong += 1
                    print(f"  [超长] {table}.{c} len={len(v)} > {limits[(table, c)]}: {v[:60]!r}…")
            for c in tc:
                v = r[c]
                if v not in (None, "") and not _F.match(str(v)):
                    bad_times.append(f"{table}.{c}={v!r}")
    if overlong:
        _fail(f"VARCHAR 超长 {overlong} 行（扩列或截断后再导）")
    if bad_times:
        _fail("纯时间列存在非 Times.FMT 脏值（先清洗）：\n  " + "\n  ".join(bad_times[:20]))
    print("  VARCHAR 超长：0 ✅    时间列脏值：0 ✅")


def migrate(snap: sqlite3.Connection, conn: pymysql.connections.Connection, batch: int = 500) -> None:
    print("== 导入 ==")
    for table in _TABLES:
        cols = _columns(snap, table)
        dt = _DATETIME_COLS.get(table, set())
        rows = snap.execute(f"SELECT {', '.join(cols)} FROM {table}").fetchall()  # noqa: S608
        placeholders = ",".join(["%s"] * len(cols))
        # 列名统一反引号（case_comment.by 为保留字；普通列名包反引号亦合法）
        sql = f"INSERT INTO {table} ({', '.join(f'`{c}`' for c in cols)}) VALUES ({placeholders})"
        n = 0
        with conn.cursor() as cur:
            cur.execute(f"DELETE FROM {table}")  # noqa: S608（表名白名单）——清掉 --recreate 时 run_migrations 预插的账本/序号行
            for i in range(0, len(rows), batch):
                chunk = [tuple((r[c] or None) if c in dt else r[c] for c in cols) for r in rows[i:i + batch]]
                cur.executemany(sql, chunk)
                n += len(chunk)
        print(f"  {table}: {n} 行")
    # 对账
    print("== 对账（SQLite vs MySQL COUNT） ==")
    ok = True
    with conn.cursor() as cur:
        for table in _TABLES:
            s = snap.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]  # noqa: S608
            cur.execute(f"SELECT COUNT(*) AS n FROM {table}")  # noqa: S608
            m = int(cur.fetchone()["n"])
            mark = "✅" if s == m else "❌"
            if s != m:
                ok = False
            print(f"  {table}: sqlite={s} mysql={m} {mark}")
    if not ok:
        _fail("对账不一致")
    print("导入完成 ✅")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sqlite", default=str(ROOT / "data" / "platform.db"))
    ap.add_argument("--recreate", action="store_true", help="DROP 重建目标库（默认要求目标库表为空）")
    args = ap.parse_args()

    src = Path(args.sqlite).resolve()
    if not src.exists():
        _fail(f"SQLite 源库不存在：{src}")
    import os

    dsn = os.environ.get("SDLC_DB_DSN", "")
    if not dsn:
        _fail("需要 SDLC_DB_DSN（mysql://user:pass@host:3306/db）")

    snap = _sqlite_snapshot(src)
    try:
        conn = _mysql_conn(dsn)
        with conn.cursor() as cur:
            if args.recreate:
                _, _, _, _, db = parse_dsn(dsn)
                cur.execute(f"DROP DATABASE IF EXISTS `{db}`")
                cur.execute(f"CREATE DATABASE `{db}` CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci")
                cur.execute(f"USE `{db}`")
                print("目标库已 DROP 重建")
            else:
                cur.execute("SELECT COUNT(*) AS n FROM information_schema.tables WHERE table_schema=DATABASE()")
                if int(cur.fetchone()["n"]) > 0:
                    _fail("目标库非空（加 --recreate 或换空库；导入不覆盖既有表）")
        if args.recreate:
            from platform_sdlc.db import Database
            from platform_sdlc.migrations import run_migrations

            run_migrations(Database(dsn))  # 全新建表（含账本空表；导入将整表复制源账本）

        dry_run(snap, _varchar_limits(conn))
        migrate(snap, conn)
        conn.close()
    finally:
        snap.close()
        # 快照临时目录由系统清理（/tmp）；此处仅关连接
    return 0


if __name__ == "__main__":
    print(json.dumps({"started": Times.now()}, ensure_ascii=False))
    raise SystemExit(main())
