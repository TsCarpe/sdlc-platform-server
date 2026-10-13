"""启动期 schema 迁移（对应 Java SchemaMigrator，八步逐条移植，幂等可重复执行）。

schema.sql 对已存在的表不加列（CREATE TABLE IF NOT EXISTS），启动时按
PRAGMA table_info 判列，缺则 ALTER TABLE ADD COLUMN。
设计依据：docs/DESIGN-v1.1 §1.3/附录 B.2、DESIGN-v2.2 D21/D22、DESIGN-v2.4 D32/D35/D39。
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path

from platform_sdlc.db import Database

log = logging.getLogger(__name__)

_SCHEMA = Path(__file__).with_name("schema.sql").read_text(encoding="utf-8")

# (表, 列, DDL)：JSON 数组列默认 '[]'，文本列默认 ''
_COLUMNS: list[tuple[str, str, str]] = [
    ("requirement", "domains", "ALTER TABLE requirement ADD COLUMN domains TEXT DEFAULT ''"),
    ("requirement", "deleted_at", "ALTER TABLE requirement ADD COLUMN deleted_at TEXT DEFAULT ''"),
    ("requirement", "risk_tier", "ALTER TABLE requirement ADD COLUMN risk_tier TEXT DEFAULT ''"),
    ("requirement", "target_repo", "ALTER TABLE requirement ADD COLUMN target_repo TEXT DEFAULT ''"),
    ("requirement", "design_final_json", "ALTER TABLE requirement ADD COLUMN design_final_json TEXT DEFAULT ''"),
    ("requirement", "target_repos", "ALTER TABLE requirement ADD COLUMN target_repos TEXT DEFAULT ''"),
    ("clarify_item", "quote", "ALTER TABLE clarify_item ADD COLUMN quote TEXT DEFAULT ''"),
    ("clarify_item", "anchor", "ALTER TABLE clarify_item ADD COLUMN anchor TEXT DEFAULT ''"),
    ("clarify_item", "status", "ALTER TABLE clarify_item ADD COLUMN status TEXT DEFAULT ''"),
    ("clarify_item", "note", "ALTER TABLE clarify_item ADD COLUMN note TEXT DEFAULT ''"),
    ("fr_point", "source", "ALTER TABLE fr_point ADD COLUMN source TEXT DEFAULT ''"),
    ("fr_point", "group_name", "ALTER TABLE fr_point ADD COLUMN group_name TEXT DEFAULT ''"),
    ("fr_point", "module", "ALTER TABLE fr_point ADD COLUMN module TEXT DEFAULT ''"),
    ("fr_point", "anchor", "ALTER TABLE fr_point ADD COLUMN anchor TEXT DEFAULT ''"),
    ("test_case", "verify_refs", "ALTER TABLE test_case ADD COLUMN verify_refs TEXT NOT NULL DEFAULT '[]'"),
    ("test_case", "defect_refs", "ALTER TABLE test_case ADD COLUMN defect_refs TEXT NOT NULL DEFAULT '[]'"),
    ("test_case", "case_no", "ALTER TABLE test_case ADD COLUMN case_no INTEGER"),
]

# v1.2 新表（存量库补建；新库由 schema.sql 负责）；v2.2 用例序号单调账本；v2.4 迭代/门禁
_TABLES: list[str] = [
    """CREATE TABLE IF NOT EXISTS media (
      req_id   TEXT NOT NULL,
      media_id TEXT NOT NULL,
      sha256   TEXT DEFAULT '',
      name     TEXT DEFAULT '',
      mime     TEXT DEFAULT '',
      bytes    INTEGER NOT NULL DEFAULT 0,
      created_at TEXT,
      PRIMARY KEY (req_id, media_id))""",
    """CREATE TABLE IF NOT EXISTS seq_case_no (
      id  INTEGER PRIMARY KEY CHECK (id = 1),
      val INTEGER NOT NULL DEFAULT 0)""",
    """CREATE TABLE IF NOT EXISTS iteration (
      name TEXT PRIMARY KEY,
      note TEXT DEFAULT '',
      created_by TEXT DEFAULT '',
      created_at TEXT)""",
    """CREATE TABLE IF NOT EXISTS gate_round (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      req_id TEXT NOT NULL,
      round_no INTEGER NOT NULL,
      status TEXT NOT NULL DEFAULT 'pending',
      checks_json TEXT DEFAULT '',
      baseline_json TEXT DEFAULT '',
      released_at TEXT,
      released_by TEXT DEFAULT '',
      created_by TEXT DEFAULT '',
      opened_at TEXT,
      UNIQUE (req_id, round_no))""",
    """CREATE TABLE IF NOT EXISTS gate_issue (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      round_id INTEGER NOT NULL,
      code TEXT NOT NULL,
      sev TEXT DEFAULT '',
      theme TEXT DEFAULT '',
      role TEXT DEFAULT '',
      scene TEXT DEFAULT '',
      quote TEXT DEFAULT '',
      loc TEXT DEFAULT '',
      basis TEXT DEFAULT '',
      div_flag INTEGER DEFAULT 0,
      tradeoff INTEGER DEFAULT 0,
      merged_unit INTEGER DEFAULT 0,
      filed INTEGER DEFAULT 0,
      verdict TEXT DEFAULT '',
      verdict_note TEXT DEFAULT '',
      verdict_by TEXT DEFAULT '',
      verdict_at TEXT,
      rev_location TEXT DEFAULT '',
      rev_status TEXT DEFAULT '')""",
    "CREATE INDEX IF NOT EXISTS idx_gate_issue_round ON gate_issue(round_id)",
]


def run_migrations(db: Database) -> None:
    db.executescript(_SCHEMA)  # 建表（IF NOT EXISTS，对应 spring.sql.init，先于判列迁移）
    for table, column, ddl in _COLUMNS:
        if column not in _columns_of(db, table):
            db.execute(ddl)
            log.info("schema 迁移：%s 增加列 %s", table, column)
    for ddl in _TABLES:
        db.execute(ddl)
    # 一次性迁移走账本（AUDIT D2-2）：已应用即跳过——原「每次启动全量空转」（_backfill_case_no
    # 的 SELECT JOIN 全表扫等）随库增长线性劣化。顺序固定：case_no 回填先于序号账本初始化。
    steps: list[tuple[str, Callable[[Database], None]]] = [
        ("backfill_clarify_status", _backfill_clarify_status),
        ("backfill_case_no", _backfill_case_no),
        ("init_case_no_seq", _init_case_no_seq),
        ("backfill_iterations", _backfill_iterations),
        ("backfill_target_repos", _backfill_target_repos),
        ("migrate_gate_values", _migrate_gate_values),
    ]
    for name, fn in steps:
        if _migration_applied(db, name):
            continue
        fn(db)
        db.execute("INSERT OR IGNORE INTO schema_version(name, applied_at) VALUES(?, datetime('now', 'localtime'))", (name,))
        log.info("迁移账本：步骤 %s 已应用并登记", name)
    # 唯一索引不入账本：存量脏数据时有意跳过（每次启动重试，干净后建；CREATE IF NOT EXISTS 幂等）
    _try_unique_req_iter_index(db)


def _migration_applied(db: Database, name: str) -> bool:
    return bool(db.query_scalar("SELECT EXISTS(SELECT 1 FROM schema_version WHERE name=?)", (name,)))


def _columns_of(db: Database, table: str) -> list[str]:
    rows = db.query_all(f"PRAGMA table_info({table})")
    return [str(row["name"]) for row in rows if row["name"] is not None]


def _backfill_clarify_status(db: Database) -> None:
    """v2.2 D22：已有非空答复的行补 status='answered'，此后读写只认 status。"""
    n = db.execute("UPDATE clarify_item SET status='answered' WHERE status='' AND answer!=''")
    if n > 0:
        log.info("澄清处置状态回填：%d 条已答行补 status=answered", n)


def _backfill_case_no(db: Database) -> None:
    """v2.2 全局用例编号回填（评测 P0-4）：只给可见用例占号；排序 created_at,id（非 rowid 红线）。
    逐行 UPDATE 属启动期一次性迁移，豁免「循环体内无数据库查询」规范；行值各异无法批量。"""
    rows = db.query_all(
        "SELECT tc.req_id, tc.id FROM test_case tc JOIN requirement r ON r.id=tc.req_id "
        "WHERE tc.case_no IS NULL AND r.deleted_at='' ORDER BY tc.created_at, tc.id"
    )
    if not rows:
        return
    max_no = db.query_scalar("SELECT MAX(case_no) FROM test_case")
    nxt = int(max_no) if max_no is not None else 0
    for row in rows:
        nxt += 1
        db.execute(
            "UPDATE test_case SET case_no=? WHERE req_id=? AND id=? AND case_no IS NULL",
            (nxt, row["req_id"], row["id"]),
        )
    log.info("全局用例编号回填：%d 条（#%d 起）", len(rows), nxt - len(rows) + 1)


def _init_case_no_seq(db: Database) -> None:
    """单调账本初始化（幂等）：仅当账本为空时以当前 MAX(case_no) 起账。"""
    db.execute(
        "INSERT INTO seq_case_no(id, val) SELECT 1, COALESCE((SELECT MAX(case_no) FROM test_case), 0) "
        "WHERE NOT EXISTS (SELECT 1 FROM seq_case_no WHERE id = 1)"
    )


def _backfill_iterations(db: Database) -> None:
    """v2.4 D32：存量存活需求的非空 iter 建成迭代行（created_at 取最早建卡时间）；已存在跳过。"""
    n = db.execute(
        "INSERT INTO iteration(name, created_by, created_at) "
        "SELECT r.iter, '', MIN(r.created_at) FROM requirement r "
        "WHERE r.iter!='' AND r.deleted_at='' AND r.iter NOT IN (SELECT name FROM iteration) "
        "GROUP BY r.iter"
    )
    if n > 0:
        log.info("迭代回填：%d 个存量 iter 建为迭代行", n)


def _backfill_target_repos(db: Database) -> None:
    """v2.4 D39 多目标仓回填：旧单仓 → target_repos 单条目（kind 按前缀 http 推断）；幂等。"""
    n = db.execute(
        "UPDATE requirement SET target_repos = "
        "CASE WHEN target_repo LIKE 'http%' THEN "
        '  \'[{"role":"","kind":"github","ref":"\' || REPLACE(target_repo,\'"\',\'\') || \'","branch":""}]\' '
        "ELSE "
        '  \'[{"role":"","kind":"local","ref":"\' || REPLACE(target_repo,\'"\',\'\') || \'","branch":""}]\' '
        "END WHERE target_repo!='' AND (target_repos='' OR target_repos IS NULL)"
    )
    if n > 0:
        log.info("多目标仓回填：%d 行 target_repo → target_repos", n)


def _migrate_gate_values(db: Database) -> None:
    """批次6（AUDIT D2-3，用户裁决）：gate 域落库值英文化——round 待裁决→pending/已放行→released、
    verdict 落改→revise/驳回→reject/存疑→query、rev_status 待执行→pending/已执行→done。
    幂等（按中文值条件 UPDATE，转后不再命中）；读面推导值「备案」不落库、checks/baseline JSON
    不含状态值，均无需迁移。中文口径保留在文案与 ER 图。"""
    n = db.execute("UPDATE gate_round SET status='released' WHERE status='已放行'")
    n += db.execute("UPDATE gate_round SET status='pending' WHERE status='待裁决'")
    n += db.execute("UPDATE gate_issue SET verdict='revise' WHERE verdict='落改'")
    n += db.execute("UPDATE gate_issue SET verdict='reject' WHERE verdict='驳回'")
    n += db.execute("UPDATE gate_issue SET verdict='query' WHERE verdict='存疑'")
    n += db.execute("UPDATE gate_issue SET rev_status='pending' WHERE rev_status='待执行'")
    n += db.execute("UPDATE gate_issue SET rev_status='done' WHERE rev_status='已执行'")
    if n > 0:
        log.info("gate 值英文化迁移：%d 行转换（中文→英文枚举）", n)


def _try_unique_req_iter_index(db: Database) -> None:
    """v2.4 D32 存活需求按 iter 唯一的 partial unique index；存量脏数据只告警跳过（服务层守卫仍生效）。"""
    dup = db.query_scalar(
        "SELECT COUNT(*) FROM (SELECT iter FROM requirement WHERE iter!='' AND deleted_at='' GROUP BY iter HAVING COUNT(*)>1)"
    )
    if dup is not None and int(dup) > 0:
        log.warning("存量数据存在同迭代多需求绑定（%s 组），跳过 1:1 唯一索引创建（服务层守卫仍生效）", dup)
        return
    try:
        db.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_req_iter_alive ON requirement(iter) WHERE deleted_at='' AND iter!=''")
    except Exception as e:
        # 与 Java 一致：索引创建失败不阻塞启动
        log.warning("1:1 唯一索引创建失败（不影响启动）：%s", e)
