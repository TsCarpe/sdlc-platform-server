"""SQLite 单写者访问（对应 Java Hikari pool=1 + JdbcTemplate + @Transactional）。

- 全局单连接（check_same_thread=False），RLock 保护；
- autocommit（isolation_level=None），与 JdbcTemplate 每语句提交一致；
- `tx()` = 服务操作粒度事务边界：持锁 + 显式 BEGIN/COMMIT，复刻
  「@Transactional 持连接整个方法」的 TOCTOU 防护（如 settleTerminal 的查-改窗口）。
  仓储内单条写无需 tx（autocommit）；多步写一律经服务层 tx()。
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

Rows = list[sqlite3.Row]


class Database:
    def __init__(self, db_path: Path) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._tx_depth = 0  # 事务嵌套深度：内层 tx() 加入外层事务（对应 Spring REQUIRED 传播）

    @property
    def lock(self) -> threading.RLock:
        return self._lock

    def execute(self, sql: str, params: tuple[Any, ...] | list[Any] = ()) -> int:
        """单条写/DDL，返回受影响行数（对应 jdbc.update 的返回值语义）。"""
        with self._lock:
            cur = self._conn.execute(sql, params)
            return cur.rowcount

    def insert(self, sql: str, params: tuple[Any, ...] | list[Any] = ()) -> int:
        """单条 INSERT，返回 lastrowid（对应自增主键回读，避免 SELECT MAX 竞态）。"""
        with self._lock:
            cur = self._conn.execute(sql, params)
            return int(cur.lastrowid or 0)

    def query_all(self, sql: str, params: tuple[Any, ...] | list[Any] = ()) -> Rows:
        with self._lock:
            return list(self._conn.execute(sql, params).fetchall())

    def query_one(self, sql: str, params: tuple[Any, ...] | list[Any] = ()) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute(sql, params).fetchone()

    def query_scalar(self, sql: str, params: tuple[Any, ...] | list[Any] = ()) -> Any:
        row = self.query_one(sql, params)
        return row[0] if row is not None else None

    def executescript(self, script: str) -> None:
        """多语句脚本（schema.sql 建表），启动期专用。"""
        with self._lock:
            self._conn.executescript(script)

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        """服务操作粒度事务：多步读-改-写全程持锁，任何异常回滚。
        可重入：嵌套调用加入外层事务，COMMIT/ROLLBACK 由最外层负责（RLock 同线程）。"""
        with self._lock:
            if self._tx_depth > 0:
                self._tx_depth += 1
                try:
                    yield self._conn
                finally:
                    self._tx_depth -= 1
                return
            self._conn.execute("BEGIN")
            self._tx_depth = 1
            try:
                yield self._conn
                self._conn.execute("COMMIT")
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            finally:
                self._tx_depth = 0
