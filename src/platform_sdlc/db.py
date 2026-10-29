"""MySQL 数据层适配（EXEC-PLAN-K8s 阶段 2；对应 Java Hikari pool + JdbcTemplate + @Transactional）。

- PyMySQL + DBUtils 连接池；**RLock 全局串行保留**（K8s replicas=1 / uvicorn 单 worker 硬约束）：
  服务层 tx() 的「SELECT 守卫→判断→UPDATE」TOCTOU 防护依赖全程持锁——MySQL 快照读不加锁，
  退役锁须逐处 SELECT FOR UPDATE 审计（远期优化项），当前并发模型与 SQLite 时期逐位一致。
- autocommit=True 连接级（对应 sqlite3 isolation_level=None）：仓储内单条写无需 tx；
  多步写一律经服务层 tx()。
- 占位符/字面 % 适配：全库 SQL 保持 `?` 占位符书写（与 SQLite 时期一致），本层入口统一转换；
  带参时**先转义字面 % 再换占位符**（PyMySQL 带参执行走 % 插值，字面 % 会炸，如 LIKE 'P%'）。
- CLIENT.FOUND_ROWS：rowcount = matched 行数（对齐 sqlite3 语义；同值重写不返回 0——
  set_disposition 重复提交依赖此行为）。
- tx()：RLock + 深度计数可重入（Spring REQUIRED 语义），事务连接存实例槽 _tx_conn
  （RLock 全局串行下同时至多一个事务，实例槽即安全）。**规约：tx 体内禁止 await/起线程**。
- Row：dict 子类，兼容 row["col"] 键访问与 int 位置下标（对齐 sqlite3.Row 消费面）。
- DATETIME 出口转换：cursor.description 类型码 DATETIME/TIMESTAMP/DATE 的列值转 Times.FMT
  字符串——models/出参契约/Python 字符串比较全链路零感知（写入侧字符串参数 MySQL 隐式转换）。
"""

from __future__ import annotations

import threading
from collections.abc import Generator, Sequence
from contextlib import contextmanager, suppress
from datetime import date, datetime
from typing import Any
from urllib.parse import unquote, urlparse

import pymysql
import pymysql.connections
from dbutils.pooled_db import PooledDB, PooledDedicatedDBConnection, PooledSharedDBConnection  # pyright: ignore[reportMissingTypeStubs]

from platform_sdlc.common import Times

# 池借连接的运行时类型（DBUtils 包装层，duck-type 透传 cursor/begin/commit/rollback/close）
DbConn = PooledSharedDBConnection | PooledDedicatedDBConnection

# cursor.description 类型码（pymysql.constants.FIELD_TYPE）：12=DATETIME, 7=TIMESTAMP, 10=DATE
_DATETIME_FIELD_CODES = frozenset({12, 7, 10})

_DSN_HINT = (
    "SDLC_DB_DSN 未配置（形如 mysql://user:pass@host:3306/sdlc）。"
    "本地开发：docker compose -f compose/mysql.dev.yml up -d 后写入 .env；"
    "K8s：经 Secret 注入（漏配应 crash-loop 可见，不给静默默认值）。"
)


def parse_dsn(dsn: str) -> tuple[str, int, str, str, str]:
    """解析 mysql://user:pass@host:port/db → (host, port, user, password, database)；密码做百分号解码。"""
    u = urlparse(dsn)
    database = u.path.lstrip("/")
    if u.scheme != "mysql" or not u.hostname or not u.username or not database:
        raise ValueError(_DSN_HINT)
    return u.hostname, u.port or 3306, u.username, unquote(u.password or ""), database


def dsn_display(dsn: str) -> str:
    """启动日志用脱敏形：user@host:port/db（不带密码）。"""
    host, port, user, _, database = parse_dsn(dsn)
    return f"{user}@{host}:{port}/{database}"


class Row(dict[str, Any]):
    """对齐 sqlite3.Row 消费面：row["col"] 键访问 + int 位置下标（DictCursor 列序即 SELECT 序）。"""

    def __getitem__(self, key: str | int) -> Any:
        if isinstance(key, int):
            return tuple(self.values())[key]
        return super().__getitem__(key)


Rows = list[Row]


def _split_script(script: str) -> list[str]:
    """schema.sql 拆分：整行 `--` 注释跳过，行尾 `;` 切分（护栏：注释一律整行、字符串字面量禁含 ASCII `;`）。"""
    stmts: list[str] = []
    buf: list[str] = []
    for line in script.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("--"):
            continue
        buf.append(line)
        if stripped.endswith(";"):
            stmt = "\n".join(buf).strip().removesuffix(";").strip()
            if stmt:
                stmts.append(stmt)
            buf = []
    tail = "\n".join(buf).strip()
    if tail:
        stmts.append(tail)
    return stmts


def _bind(cur: pymysql.cursors.Cursor, sql: str, params: Sequence[Any]) -> None:
    """执行单条：带参时先转义字面 % 再换占位符（顺序不可颠倒）；无参传 None 跳过 % 插值。"""
    if params:
        cur.execute(sql.replace("%", "%%").replace("?", "%s"), tuple(params))
    else:
        cur.execute(sql, None)


def _rows(desc: Sequence[Sequence[Any]] | None, raw: list[dict[str, Any]]) -> Rows:
    """DictCursor 行 → Row；DATETIME 族列（description 类型码 12/7/10）值转 Times.FMT 字符串。"""
    columns = [str(d[0]) for d in desc or ()]
    dt_idx = [i for i, d in enumerate(desc or ()) if d[1] in _DATETIME_FIELD_CODES]
    out: Rows = []
    for r in raw:
        if dt_idx:
            values = list(r.values())
            for i in dt_idx:
                v = values[i]
                if isinstance(v, (datetime, date)):
                    values[i] = v.strftime(Times.FMT)
            out.append(Row(zip(columns, values, strict=True)))
        else:
            out.append(Row(zip(columns, r.values(), strict=True)))
    return out


class Database:
    def __init__(self, dsn: str) -> None:
        if not dsn:
            raise ValueError(_DSN_HINT)
        host, port, user, password, database = parse_dsn(dsn)
        self._lock = threading.RLock()
        # FOUND_ROWS：rowcount=matched（sqlite3 语义）；ping=1：借出即 ping，覆盖 MySQL 重启/wait_timeout 断连；
        # read/write_timeout：语句级 socket 超时，防一条挂死查询把 RLock 连同全站冻结
        try:
            self._pool = PooledDB(
                creator=pymysql,  # DB-API 2 模块（探测 .threadsafety/.connect；传 connect 函数会被判 0）
                mincached=1,  # 启动即建一条连接（DSN 错误在构造期立即暴露）
                maxconnections=5,
                host=host,
                port=port,
                user=user,
                password=password,
                database=database,
                charset="utf8mb4",
                autocommit=True,
                connect_timeout=10,
                read_timeout=60,
                write_timeout=60,
                client_flag=pymysql.connections.CLIENT.FOUND_ROWS,
                ping=1,
            )
        except pymysql.err.OperationalError as e:
            code = e.args[0] if e.args else 0
            if code == 1049:
                raise ValueError(
                    f"MySQL 库不存在（{dsn_display(dsn)}）：生产找 DBA 预建；本地检查 compose mysql.dev.yml 的 MYSQL_DATABASE。"
                ) from e
            if code == 1045:
                raise ValueError(f"MySQL 凭据错误（{dsn_display(dsn)}）：检查 SDLC_DB_DSN / K8s Secret。") from e
            raise
        self._tx_depth = 0  # 事务嵌套深度：内层 tx() 加入外层事务（对应 Spring REQUIRED 传播）
        self._tx_conn: DbConn | None = None  # 事务期独占连接（实例槽，RLock 串行下同时至多一个）
        self._closed = False

    @contextmanager
    def _session(self) -> Generator[DbConn, None, None]:
        """语句级连接：事务期复用 _tx_conn，否则池借池还。调用方必须已持 RLock。"""
        if self._tx_conn is not None:
            yield self._tx_conn
            return
        conn = self._pool.connection()
        try:
            yield conn
        finally:
            conn.close()  # DBUtils 语义：归还池而非断开

    def execute(self, sql: str, params: tuple[Any, ...] | list[Any] = ()) -> int:
        """单条写/DDL，返回受影响行数（matched 语义，对应 jdbc.update）。"""
        with self._lock, self._session() as conn:
            cur = conn.cursor()
            try:
                _bind(cur, sql, params)
                return int(cur.rowcount)
            finally:
                cur.close()

    def insert(self, sql: str, params: tuple[Any, ...] | list[Any] = ()) -> int:
        """单条 INSERT，返回 lastrowid（自增主键回读，避免 SELECT MAX 竞态）。"""
        with self._lock, self._session() as conn:
            cur = conn.cursor()
            try:
                _bind(cur, sql, params)
                return int(cur.lastrowid or 0)
            finally:
                cur.close()

    def executemany(self, sql: str, seq: list[tuple[Any, ...]]) -> None:
        """批量写：空批次直通；须在服务层 tx 事务边界内使用（事务连接上执行，
        PyMySQL INSERT multi-values 自动 ~1MB 分片、UPDATE 逐条回退，本库不依赖返回值）。"""
        if not seq:
            return
        with self._lock, self._session() as conn:
            cur = conn.cursor()
            try:
                cur.executemany(sql.replace("%", "%%").replace("?", "%s"), [tuple(r) for r in seq])
            finally:
                cur.close()

    def query_all(self, sql: str, params: tuple[Any, ...] | list[Any] = ()) -> Rows:
        with self._lock, self._session() as conn:
            cur = conn.cursor(pymysql.cursors.DictCursor)
            try:
                _bind(cur, sql, params)
                return _rows(cur.description, cur.fetchall())
            finally:
                cur.close()

    def query_one(self, sql: str, params: tuple[Any, ...] | list[Any] = ()) -> Row | None:
        with self._lock, self._session() as conn:
            cur = conn.cursor(pymysql.cursors.DictCursor)
            try:
                _bind(cur, sql, params)
                rows = _rows(cur.description, cur.fetchall())
                return rows[0] if rows else None
            finally:
                cur.close()

    def query_scalar(self, sql: str, params: tuple[Any, ...] | list[Any] = ()) -> Any:
        row = self.query_one(sql, params)
        return row[0] if row is not None else None

    def executescript(self, script: str) -> None:
        """多语句 DDL 脚本（schema.sql 建表），启动期专用。
        幂等：容忍 1050（表已存在）/1061（索引已存在——MySQL 无 CREATE INDEX IF NOT EXISTS）。"""
        assert self._tx_depth == 0, "executescript 禁止在 tx 内：MySQL DDL 隐式提交会拆掉外层事务"
        with self._lock:
            for stmt in _split_script(script):
                with self._session() as conn:
                    cur = conn.cursor()
                    try:
                        cur.execute(stmt, None)
                    except pymysql.err.OperationalError as e:
                        if e.args and e.args[0] in (1050, 1061):
                            continue
                        raise
                    finally:
                        cur.close()

    @contextmanager
    def tx(self) -> Generator[None, None, None]:
        """服务操作粒度事务：RLock 全程持锁 + 实例槽事务连接，任何异常回滚。
        可重入：嵌套调用加入外层事务，COMMIT/ROLLBACK 由最外层负责（Spring REQUIRED）。
        规约：tx 体内禁止 await / 起线程（锁与事务连接均不可跨执行流）。"""
        with self._lock:
            if self._tx_depth > 0:
                self._tx_depth += 1
                try:
                    yield
                finally:
                    self._tx_depth -= 1
                return
            conn = self._pool.connection()
            self._tx_conn = conn
            self._tx_depth = 1
            try:
                conn.begin()  # autocommit 连接上的显式事务（COMMIT 后回到 autocommit）
                yield
                conn.commit()
            except BaseException:
                with suppress(Exception):  # 连接已死时 ROLLBACK 无意义；连接还池后由 ping 丢弃重建
                    conn.rollback()
                raise
            finally:
                self._tx_depth = 0
                self._tx_conn = None
                conn.close()  # 归还池

    def close(self) -> None:
        """关闭连接池（幂等——lifespan finally 与测试 teardown 可能都调；
        未关连接会占 max_connections 并卡 DROP DATABASE 的 MDL）。"""
        with self._lock:
            if not self._closed:
                self._pool.close()
                self._closed = True

    def ping(self) -> None:
        """连通性探测（K8s readiness 专用）：借出即 ping（PooledDB ping=1），不取全局 RLock——
        长事务持锁期间 readiness 不应排队被误判摘流；池满即抛错不阻塞。池已关闭/借出失败抛异常，
        由端点转 503（PooledDB.close() 后 connection() 会静默新开连接，_closed 检查必要）。"""
        if self._closed:
            raise RuntimeError("连接池已关闭")
        conn = self._pool.connection()
        conn.close()  # DBUtils 语义：归还池（借出时的 ping=1 已完成探测）
