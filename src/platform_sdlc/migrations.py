"""启动期 schema 迁移（对应 Java SchemaMigrator，八步逐条移植，幂等可重复执行）。

schema.sql 对已存在的表不加列（CREATE TABLE IF NOT EXISTS），启动时按
information_schema.columns 判列，缺则 ALTER TABLE ADD COLUMN。
设计依据：docs/DESIGN-v1.1 §1.3/附录 B.2、DESIGN-v2.2 D21/D22、DESIGN-v2.4 D32/D35/D39。
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pymysql.err

from platform_sdlc.common import Times, is_json_list, is_json_obj
from platform_sdlc.db import Database
from platform_sdlc.models import BLOCK_ATTR_EXCLUDED as _BLOCK_ATTR_EXCLUDED

log = logging.getLogger(__name__)

_SCHEMA = Path(__file__).with_name("schema.sql").read_text(encoding="utf-8")

# (表, 列, DDL)：JSON 数组列默认 '[]'，文本列默认 ''
# MySQL 化（新库由 schema.sql 建全列、此处恒空转；保留为 SQLite 存量库导入前的补列保底——
# 类型规则与 schema.sql 一致：标识符列 COLLATE utf8mb4_bin、JSON 数组 DEFAULT ('[]')、纯时间列 DATETIME）
_COLUMNS: list[tuple[str, str, str]] = [
    ("requirement", "domains", "ALTER TABLE requirement ADD COLUMN domains VARCHAR(1024) DEFAULT ''"),
    ("requirement", "deleted_at", "ALTER TABLE requirement ADD COLUMN deleted_at VARCHAR(32) DEFAULT ''"),
    ("requirement", "risk_tier", "ALTER TABLE requirement ADD COLUMN risk_tier VARCHAR(8) DEFAULT ''"),
    ("requirement", "target_repo", "ALTER TABLE requirement ADD COLUMN target_repo VARCHAR(2048) DEFAULT ''"),
    ("requirement", "design_final_json", "ALTER TABLE requirement ADD COLUMN design_final_json MEDIUMTEXT DEFAULT ('')"),
    ("requirement", "target_repos", "ALTER TABLE requirement ADD COLUMN target_repos MEDIUMTEXT DEFAULT ('')"),
    ("clarify_item", "quote", "ALTER TABLE clarify_item ADD COLUMN quote TEXT DEFAULT ('')"),
    ("clarify_item", "anchor", "ALTER TABLE clarify_item ADD COLUMN anchor VARCHAR(64) COLLATE utf8mb4_bin DEFAULT ''"),
    ("clarify_item", "status", "ALTER TABLE clarify_item ADD COLUMN status VARCHAR(16) DEFAULT ''"),
    ("clarify_item", "note", "ALTER TABLE clarify_item ADD COLUMN note TEXT DEFAULT ('')"),
    # v4.1（DESIGN-v4 §6.3）：澄清项来源三值 skill|incremental|local——判列补列默认空串，
    # 存量行由账本步骤 backfill_clarify_origin 回填 'skill'（否则「按来源合并」永远匹配不到存量行）
    ("clarify_item", "origin", "ALTER TABLE clarify_item ADD COLUMN origin VARCHAR(16) DEFAULT ''"),
    # v4.8（DESIGN-v4 §4.1）：评论升级回链——被升级的评论可跳回原讨论上下文；存量行无来源评论，无回填
    ("clarify_item", "src_comment_id", "ALTER TABLE clarify_item ADD COLUMN src_comment_id BIGINT"),
    # v1.6（DESIGN-v5 §6.1/§6.3）：选择题化作答契约——options=选项快照（JSON 数组串）、
    # module/layer=分层归属（layer 1-7 体系，§6.2）。默认空串/空选项，存量行零回填零迁移
    # P1-6（2026-10 三仓评估）：retry 复用同一 task id——attempt 单调递增作 fencing，迟到/重复的
    # done/failed 只有携带当前 attempt 才能置终态（旧执行器跨 retry 晚到不得覆盖新 attempt）
    ("task", "attempt", "ALTER TABLE task ADD COLUMN attempt INT NOT NULL DEFAULT 0"),
    # P1-9（2026-10 三仓评估）：评审扇出留痕——issues 头 reviewMode/fanout/contextMode 在 ingest
    # 机械解析落库，UI 可见「降级评审」，A/B 级 degraded 在 ingest 被拒（A+B 策略）
    ("gate_round", "review_mode", "ALTER TABLE gate_round ADD COLUMN review_mode VARCHAR(32) DEFAULT ''"),
    ("gate_round", "fanout", "ALTER TABLE gate_round ADD COLUMN fanout INT NOT NULL DEFAULT 0"),
    ("gate_round", "context_mode", "ALTER TABLE gate_round ADD COLUMN context_mode VARCHAR(32) DEFAULT ''"),
    ("clarify_item", "options", "ALTER TABLE clarify_item ADD COLUMN options TEXT DEFAULT ('')"),
    ("clarify_item", "module", "ALTER TABLE clarify_item ADD COLUMN module VARCHAR(255) DEFAULT ''"),
    ("clarify_item", "layer", "ALTER TABLE clarify_item ADD COLUMN layer VARCHAR(16) DEFAULT ''"),
    # v4.12（DESIGN-v4 §5.5/§7）：content_rev 单调递增不回退（仅作展示计数，变更判定查变更单不比 rev）；
    # blocks_json = draft 全量块数组留底（confirm 应用面的顺序来源——items 只存变更行不含新序）
    ("requirement", "content_rev", "ALTER TABLE requirement ADD COLUMN content_rev BIGINT NOT NULL DEFAULT 0"),
    ("req_change", "blocks_json", "ALTER TABLE req_change ADD COLUMN blocks_json MEDIUMTEXT DEFAULT ('')"),
    # v4.13（DESIGN-v4 §5.6）：snapshot_json = confirm 时点快照（baseline 块序 + 重开澄清原值——
    # 放弃的全量逆向回滚依据）；aborted_by/at = 放弃留痕
    ("req_change", "snapshot_json", "ALTER TABLE req_change ADD COLUMN snapshot_json MEDIUMTEXT DEFAULT ('')"),
    # v4.17（DESIGN-v4 §10）：变更单 kind 三值 change（人提）| reconcile（迁移对账，未发布卡
    # confirm=应用+保持原状态的状态域豁免）| rebaseline（已发布 text 存量卡，该 kind 放行一次全量）
    ("req_change", "kind", "ALTER TABLE req_change ADD COLUMN kind VARCHAR(16) DEFAULT 'change'"),
    ("req_change", "aborted_by", "ALTER TABLE req_change ADD COLUMN aborted_by VARCHAR(255) DEFAULT ''"),
    ("req_change", "aborted_at", "ALTER TABLE req_change ADD COLUMN aborted_at DATETIME"),
    # v4.19：merged_at = 增量合并采纳时点（abort 守卫据真值判「已采纳」——此前以草稿不存在近似，
    # 而采纳即删草稿，近似恒空转：merge 后放弃会产出「块回滚、产物保持合并态」的分裂数据）
    ("req_change", "merged_at", "ALTER TABLE req_change ADD COLUMN merged_at DATETIME"),
    ("fr_point", "source", "ALTER TABLE fr_point ADD COLUMN source VARCHAR(32) DEFAULT ''"),
    ("fr_point", "group_name", "ALTER TABLE fr_point ADD COLUMN group_name VARCHAR(255) DEFAULT ''"),
    ("fr_point", "module", "ALTER TABLE fr_point ADD COLUMN module VARCHAR(255) DEFAULT ''"),
    ("fr_point", "anchor", "ALTER TABLE fr_point ADD COLUMN anchor VARCHAR(64) COLLATE utf8mb4_bin DEFAULT ''"),
    ("test_case", "verify_refs", "ALTER TABLE test_case ADD COLUMN verify_refs TEXT NOT NULL DEFAULT ('[]')"),
    ("test_case", "defect_refs", "ALTER TABLE test_case ADD COLUMN defect_refs TEXT NOT NULL DEFAULT ('[]')"),
    ("test_case", "case_no", "ALTER TABLE test_case ADD COLUMN case_no BIGINT"),
]

# v1.2 新表（存量库补建；新库由 schema.sql 负责）；v2.2 用例序号单调账本；v2.4 迭代/门禁。
# MySQL 化：索引为裸 CREATE INDEX（重复执行的 1061 由下方循环捕获容忍——MySQL 无 IF NOT EXISTS）
_TABLES: list[str] = [
    """CREATE TABLE IF NOT EXISTS media (
      req_id VARCHAR(32) COLLATE utf8mb4_bin NOT NULL,
      media_id VARCHAR(64) COLLATE utf8mb4_bin NOT NULL,
      sha256 VARCHAR(64) DEFAULT '',
      name VARCHAR(255) DEFAULT '',
      mime VARCHAR(128) DEFAULT '',
      bytes BIGINT NOT NULL DEFAULT 0,
      created_at DATETIME NULL,
      PRIMARY KEY (req_id, media_id)) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci""",
    # v4.11（DESIGN-v4 §5.2）：变更单两表——新表无存量可搬，判表补表幂等即可
    """CREATE TABLE IF NOT EXISTS req_change (
      id BIGINT AUTO_INCREMENT PRIMARY KEY,
      req_id VARCHAR(32) COLLATE utf8mb4_bin NOT NULL,
      title VARCHAR(512) DEFAULT '',
      detail TEXT DEFAULT (''),
      proposed_by VARCHAR(255) DEFAULT '',
      confirmed_by VARCHAR(255) DEFAULT '',
      status VARCHAR(16) NOT NULL DEFAULT 'draft',
      impact_json MEDIUMTEXT DEFAULT (''),
      created_at DATETIME, confirmed_at DATETIME) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci""",
    """CREATE TABLE IF NOT EXISTS req_change_item (
      id BIGINT AUTO_INCREMENT PRIMARY KEY,
      change_id BIGINT NOT NULL,
      block_id VARCHAR(64) COLLATE utf8mb4_bin NOT NULL,
      op VARCHAR(8) NOT NULL,
      old_text MEDIUMTEXT DEFAULT (''), new_text MEDIUMTEXT DEFAULT (''),
      old_attrs TEXT DEFAULT (''), new_attrs TEXT DEFAULT ('')) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci""",
    "CREATE INDEX idx_req_change_req ON req_change(req_id)",
    "CREATE INDEX idx_req_change_item_change ON req_change_item(change_id)",
    """CREATE TABLE IF NOT EXISTS seq_case_no (
      id  INT NOT NULL,
      val BIGINT NOT NULL DEFAULT 0,
      PRIMARY KEY (id),
      CHECK (id = 1)) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci""",
    """CREATE TABLE IF NOT EXISTS iteration (
      name VARCHAR(128) COLLATE utf8mb4_bin NOT NULL,
      note TEXT DEFAULT (''),
      created_by VARCHAR(255) DEFAULT '',
      created_at DATETIME,
      PRIMARY KEY (name)) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci""",
    """CREATE TABLE IF NOT EXISTS gate_round (
      id BIGINT AUTO_INCREMENT PRIMARY KEY,
      req_id VARCHAR(32) COLLATE utf8mb4_bin NOT NULL,
      round_no INT NOT NULL,
      status VARCHAR(16) NOT NULL DEFAULT 'pending',
      checks_json MEDIUMTEXT DEFAULT (''),
      baseline_json MEDIUMTEXT DEFAULT (''),
      released_at DATETIME,
      released_by VARCHAR(255) DEFAULT '',
      created_by VARCHAR(255) DEFAULT '',
      opened_at DATETIME,
      UNIQUE KEY uq_gate_round_req_round (req_id, round_no)) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci""",
    """CREATE TABLE IF NOT EXISTS gate_issue (
      id BIGINT AUTO_INCREMENT PRIMARY KEY,
      round_id BIGINT NOT NULL,
      code VARCHAR(64) NOT NULL,
      sev VARCHAR(8) DEFAULT '',
      theme VARCHAR(255) DEFAULT '',
      role VARCHAR(64) DEFAULT '',
      scene VARCHAR(255) DEFAULT '',
      quote TEXT DEFAULT (''),
      loc VARCHAR(255) DEFAULT '',
      basis TEXT DEFAULT (''),
      div_flag TINYINT DEFAULT 0,
      tradeoff TINYINT DEFAULT 0,
      merged_unit INT DEFAULT 0,
      filed TINYINT DEFAULT 0,
      verdict VARCHAR(16) DEFAULT '',
      verdict_note TEXT DEFAULT (''),
      verdict_by VARCHAR(255) DEFAULT '',
      verdict_at DATETIME,
      rev_location VARCHAR(255) DEFAULT '',
      rev_status VARCHAR(16) DEFAULT '') ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci""",
    "CREATE INDEX idx_gate_issue_round ON gate_issue(round_id)",
    # v4.1（DESIGN-v4 §2.2/§4.2）：块表 + 评论表（新库由 schema.sql 负责，此处存量库补建；DDL 同文）
    """CREATE TABLE IF NOT EXISTS req_block (
      req_id VARCHAR(32) COLLATE utf8mb4_bin NOT NULL,
      block_id VARCHAR(64) COLLATE utf8mb4_bin NOT NULL,
      seq BIGINT NOT NULL,
      type VARCHAR(32) NOT NULL,
      text MEDIUMTEXT DEFAULT (''),
      attrs_json TEXT DEFAULT (''),
      version BIGINT NOT NULL DEFAULT 1,
      updated_at DATETIME,
      updated_by VARCHAR(255),
      deleted VARCHAR(32) DEFAULT '',
      PRIMARY KEY (req_id, block_id)) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci""",
    "CREATE INDEX idx_req_block_req_seq ON req_block(req_id, seq)",
    """CREATE TABLE IF NOT EXISTS req_comment (
      id BIGINT AUTO_INCREMENT PRIMARY KEY,
      req_id VARCHAR(32) COLLATE utf8mb4_bin NOT NULL,
      block_id VARCHAR(64) COLLATE utf8mb4_bin DEFAULT '',
      origin VARCHAR(16) DEFAULT 'platform',
      external_id VARCHAR(64) COLLATE utf8mb4_bin DEFAULT '',
      author VARCHAR(255) DEFAULT '',
      text TEXT NOT NULL,
      reply_to BIGINT,
      resolved TINYINT DEFAULT 0,
      created_at DATETIME) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci""",
    "CREATE INDEX idx_req_comment_req ON req_comment(req_id)",
    "CREATE INDEX idx_req_comment_ext ON req_comment(req_id, external_id)",
]


def run_migrations(db: Database) -> None:
    db.executescript(_SCHEMA)  # 建表（IF NOT EXISTS，对应 spring.sql.init，先于判列迁移）
    cols_cache: dict[str, set[str]] = {}  # information_schema 查询比 PRAGMA 贵，按表缓存一次
    for table, column, ddl in _COLUMNS:
        if table not in cols_cache:
            cols_cache[table] = set(_columns_of(db, table))
        if column not in cols_cache[table]:
            db.execute(ddl)
            cols_cache[table].add(column)
            log.info("schema 迁移：%s 增加列 %s", table, column)
    for ddl in _TABLES:
        try:
            db.execute(ddl)
        except pymysql.err.OperationalError as e:  # 索引已存在（1061）：MySQL 无 CREATE INDEX IF NOT EXISTS
            if e.args and e.args[0] == 1061:
                continue
            raise
    # 一次性迁移走账本（AUDIT D2-2）：已应用即跳过——原「每次启动全量空转」（_backfill_case_no
    # 的 SELECT JOIN 全表扫等）随库增长线性劣化。顺序固定：case_no 回填先于序号账本初始化。
    steps: list[tuple[str, Callable[[Database], None]]] = [
        ("backfill_clarify_status", _backfill_clarify_status),
        ("backfill_case_no", _backfill_case_no),
        ("init_case_no_seq", _init_case_no_seq),
        ("backfill_iterations", _backfill_iterations),
        ("backfill_target_repos", _backfill_target_repos),
        ("migrate_gate_values", _migrate_gate_values),
        # v4.1（DESIGN-v4 §10）：origin 回填 → doc→块表搬运 → anchor 健康度登记（顺序：校验依赖块表就位）
        ("backfill_clarify_origin", _backfill_clarify_origin),
        ("migrate_doc_artifact_to_blocks", _migrate_doc_artifact_to_blocks),
        ("audit_anchor_health", _audit_anchor_health),
        ("backfill_text_cards_import", _backfill_text_cards_import),
        # v4.16（DESIGN-v4 §7/§10）：GR-3 基线 reqRev 存量回填——已放行轮的 baseline 无 reqRev 键时
        # 补当前 content_rev（近似快照；变更判定本就查变更单不比 rev，此键仅作展示对照）
        ("backfill_gate_reqrev", _backfill_gate_reqrev),
    ]
    for name, fn in steps:
        if _migration_applied(db, name):
            continue
        fn(db)
        # 普通 INSERT（不用 INSERT IGNORE——会把截断等真错误吞成 warning）；启动单线程 + 前置判断无竞态
        db.execute("INSERT INTO schema_version(name, applied_at) VALUES(?, ?)", (name, Times.now()))
        log.info("迁移账本：步骤 %s 已应用并登记", name)
    # 唯一索引不入账本：存量脏数据时有意跳过（每次启动重试，干净后建；CREATE IF NOT EXISTS 幂等）
    _try_unique_req_iter_index(db)


def _migration_applied(db: Database, name: str) -> bool:
    return bool(db.query_scalar("SELECT EXISTS(SELECT 1 FROM schema_version WHERE name=?)", (name,)))


def _columns_of(db: Database, table: str) -> list[str]:
    # 别名钉住返回列名（MySQL 对 information_schema 的元数据列名返回大写 COLUMN_NAME）
    rows = db.query_all(
        "SELECT column_name AS col FROM information_schema.columns WHERE table_schema=DATABASE() AND table_name=?",
        (table,),
    )
    return [str(row["col"]) for row in rows if row["col"] is not None]


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
        # FROM DUAL：MySQL 的 SELECT 无 FROM 子句不许带 WHERE（SQLite 允许）
        "INSERT INTO seq_case_no(id, val) SELECT 1, COALESCE((SELECT MAX(case_no) FROM test_case), 0) "
        "FROM DUAL WHERE NOT EXISTS (SELECT 1 FROM seq_case_no WHERE id = 1)"
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
        # CONCAT 替代 ||（MySQL 的 || 默认是 OR）；无参语句——LIKE 'http%' 的字面 % 走无参分支不转义
        "UPDATE requirement SET target_repos = "
        "CASE WHEN target_repo LIKE 'http%' THEN "
        '  CONCAT(\'[{"role":"","kind":"github","ref":"\', REPLACE(target_repo,\'"\',\'\'), \'","branch":""}]\') '
        "ELSE "
        '  CONCAT(\'[{"role":"","kind":"local","ref":"\', REPLACE(target_repo,\'"\',\'\'), \'","branch":""}]\') '
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


# 块 JSON 顶层键排除集契约见 models.BLOCK_ATTR_EXCLUDED（顶部 import 别名，迁移与导入落库共用单一来源）

def _backfill_clarify_origin(db: Database) -> None:
    """v4.1（DESIGN-v4 §6.3/§10）：存量澄清项 origin 回填 'skill'——判列补列默认空串会让
    「按来源合并」（重推删机器行保留 local 行）永远匹配不到存量行，每次重推累积一份。"""
    n = db.execute("UPDATE clarify_item SET origin='skill' WHERE origin=''")
    if n > 0:
        log.info("澄清项 origin 回填：%d 行 → skill", n)


def _migrate_doc_artifact_to_blocks(db: Database) -> None:
    """v4.1（DESIGN-v4 §10）：doc artifact content_json → req_block 一次性搬运，块 ID 原样（anchor 零迁移）；
    comment 块转 req_comment（origin='feishu'，thread 摊平为 reply_to 链，external_id=comment-id）；
    已发布卡只迁存储不改状态；doc artifact 行保留归档不再更新（快照时点=当年 push 时刻，与飞书现状的
    漂移属未登记变更，走三期变更单补登记）。逐块收集后批量、评论链逐条（reply_to 需 lastrowid），
    属启动期一次性迁移，豁免「循环体内无数据库查询」规范（同 _backfill_case_no 先例）。"""
    rows = db.query_all("SELECT req_id, content_json FROM artifact WHERE type='doc'")
    moved_blocks = 0
    moved_comments = 0
    for row in rows:
        try:
            raw: Any = json.loads(row["content_json"] or "")
        except json.JSONDecodeError:
            log.warning("doc artifact %s content_json 解析失败，跳过块表搬运", row["req_id"])
            continue
        if not is_json_obj(raw):
            continue
        blocks = raw.get("blocks")
        if not is_json_list(blocks) or not blocks:
            continue
        req_id = str(row["req_id"])
        block_rows: list[tuple[Any, ...]] = []
        comments: list[dict[str, Any]] = []
        seq = 0
        for b in blocks:
            if not is_json_obj(b):
                continue
            seq += 1000
            btype = str(b.get("type") or "")
            if btype == "comment":
                comments.append(b)
                continue
            attrs = {k: v for k, v in b.items() if k not in _BLOCK_ATTR_EXCLUDED}
            block_rows.append((req_id, str(b.get("id") or ""), seq, btype, str(b.get("text") or ""),
                               json.dumps(attrs, ensure_ascii=False, separators=(",", ":"))))
        with db.tx():
            db.execute("DELETE FROM req_block WHERE req_id=?", (req_id,))  # 防御性清位（账本只跑一次，正常无行）
            db.executemany(
                "INSERT INTO req_block(req_id,block_id,seq,type,text,attrs_json) VALUES(?,?,?,?,?,?)",
                block_rows,
            )
            for cb in comments:
                moved_comments += _insert_feishu_comments(db, req_id, cb)
        moved_blocks += len(block_rows)
    if moved_blocks or moved_comments:
        log.info("doc→块表迁移：%d 块 / %d 条飞书评论落库（%d 张卡）", moved_blocks, moved_comments, len(rows))


def _insert_feishu_comments(db: Database, req_id: str, cb: dict[str, Any]) -> int:
    """comment 块（parse_doc 契约：id/anchorBlockId/thread[{user,text}]/quote/reactions）→ req_comment 行：
    thread 首条为根，后续逐条 reply_to 链；parse_doc 输出无时间/解决态，created_at 置空、resolved=0。"""
    n = 0
    prev: int | None = None
    thread: list[Any] = cb.get("thread") or []
    for msg in thread:
        if not is_json_obj(msg):
            continue
        text = str(msg.get("text") or "").strip()
        if not text:
            continue
        prev = db.insert(
            "INSERT INTO req_comment(req_id,block_id,origin,external_id,author,text,reply_to,resolved,created_at)"
            " VALUES(?,?,?,?,?,?,?,0,NULL)",
            (req_id, str(cb.get("anchorBlockId") or ""), "feishu", str(cb.get("id") or ""),
             str(msg.get("user") or ""), text, prev),
        )
        n += 1
    return n


def _iter_anchor_refs(db: Database, req_id: str) -> list[tuple[str, str]]:
    """收集该卡全部产物的 anchor 引用 (来源标签, anchor 值)——启动期一次性扫描，豁免循环查询规范。"""
    refs: list[tuple[str, str]] = []
    for a in db.query_all(
        "SELECT type, content_json FROM artifact WHERE req_id=? AND type IN ('digest','audit')", (req_id,)
    ):
        try:
            data: Any = json.loads(a["content_json"] or "")
        except json.JSONDecodeError:
            continue
        if not is_json_obj(data):
            continue
        if a["type"] == "digest":
            feats: list[Any] = data.get("features") or []
            for f in feats:
                refs.append(("digest.feature", str(f.get("anchor") or "")))
            flows: list[Any] = data.get("flows") or []
            for f in flows:
                refs.append(("digest.flow", str(f.get("anchor") or "")))
            states: list[Any] = data.get("states") or []
            for s in states:
                refs.append(("digest.state", str(s.get("anchor") or "")))
        else:
            items: list[Any] = data.get("items") or []
            for i in items:
                refs.append(("audit.item", str(i.get("anchor") or "")))
    for p in db.query_all("SELECT anchor FROM fr_point WHERE req_id=?", (req_id,)):
        refs.append(("fr_point", str(p["anchor"] or "")))
    for c in db.query_all("SELECT seq, anchor FROM clarify_item WHERE req_id=?", (req_id,)):
        refs.append((f"clarify.{c['seq']}", str(c["anchor"] or "")))
    return refs


def _backfill_text_cards_import(db: Database) -> None:
    """v4-4（DESIGN-v4 §10）：存量 text 卡补排 source_import——块表空 = 首次导入（不是重导）。
    测试卡一并补排（导入无害，宽口径）；已有任务行的卡跳过（幂等）；已删卡不补。"""
    n = db.execute(
        "INSERT INTO task(req_id, type, status, payload, created_by, created_at)"
        " SELECT r.id, 'source_import', 'queued', '{\"note\":\"存量首导补排（v4 迁移）\"}',"
        " 'migration:v4', ?"
        " FROM requirement r"
        " WHERE r.source_type='text' AND r.deleted_at=''"
        " AND NOT EXISTS (SELECT 1 FROM req_block WHERE req_id=r.id)"
        " AND NOT EXISTS (SELECT 1 FROM task t WHERE t.req_id=r.id AND t.type='source_import')",
        (Times.now(),),
    )
    if n > 0:
        log.info("存量 text 卡首导补排：%d 张卡 queued source_import（导入后自动可用块模型质检）", n)


def _audit_anchor_health(db: Database) -> None:
    """v4.1（DESIGN-v4 §5.7 前置守卫的存量面）：有块表的卡，digest/audit/FR/澄清项的 anchor 空或
    不在块表 → log.warning 登记在案（脏 anchor 不静默进关键路径）；运行面强拒随三期增量合并落地。
    无块表的卡（text 存量卡）汇总一条 info——anchor 与块模型零关联属预期（§10 首导后强制重质检接上）。"""
    with_blocks = {str(r["req_id"]) for r in db.query_all("SELECT DISTINCT req_id FROM req_block")}
    total_bad = 0
    for req_id in sorted(with_blocks):
        known = {str(r["block_id"]) for r in db.query_all("SELECT block_id FROM req_block WHERE req_id=?", (req_id,))}
        bad = [(src, a) for src, a in _iter_anchor_refs(db, req_id) if not a or a not in known]
        for src, anchor in bad:
            log.warning("anchor 健康度：%s 空/幻觉 anchor（req=%s anchor=%r），已登记；运行面校验随增量合并落地",
                        src, req_id, anchor)
        total_bad += len(bad)
    no_blocks = db.query_scalar(
        "SELECT COUNT(DISTINCT id) FROM requirement WHERE deleted_at=''"
        " AND id NOT IN (SELECT DISTINCT req_id FROM req_block)")
    if no_blocks is not None and int(no_blocks) > 0:
        log.info("anchor 健康度：%s 张存活卡无块表（text 存量卡），anchor 与块模型零关联属预期，随首导后重质检接上",
                 int(no_blocks))
    if total_bad:
        log.warning("anchor 健康度汇总：%d 条脏 anchor（空/不在块表）", total_bad)


def _try_unique_req_iter_index(db: Database) -> None:
    """v2.4 D32 存活需求按 iter 唯一：MySQL 无 partial unique index，用生成列等价——
    alive_iter = CASE WHEN 未删且 iter 非空 THEN iter ELSE NULL END（NULL 不参与唯一）。
    存量脏数据只告警跳过（服务层守卫仍生效）；不进 schema.sql：executescript 先于本守护执行，
    存量 dup 库会在建索引处 1062 崩溃，「检测→跳过」语义到不了。"""
    dup = db.query_scalar(
        "SELECT COUNT(*) FROM (SELECT iter FROM requirement WHERE iter!='' AND deleted_at='' GROUP BY iter HAVING COUNT(*)>1) AS dup_iter"
    )
    if dup is not None and int(dup) > 0:
        log.warning("存量数据存在同迭代多需求绑定（%s 组），跳过 1:1 唯一索引创建（服务层守卫仍生效）", dup)
        return
    if "alive_iter" not in _columns_of(db, "requirement"):
        db.execute(
            "ALTER TABLE requirement ADD COLUMN alive_iter VARCHAR(128) COLLATE utf8mb4_bin "
            "AS (CASE WHEN deleted_at='' AND iter<>'' THEN iter ELSE NULL END) STORED"
        )
    exists = db.query_scalar(
        "SELECT COUNT(*) FROM information_schema.statistics "
        "WHERE table_schema=DATABASE() AND table_name='requirement' AND index_name='uq_req_iter_alive'"
    )
    if exists is not None and int(exists) > 0:
        return
    try:
        db.execute("CREATE UNIQUE INDEX uq_req_iter_alive ON requirement(alive_iter)")
    except Exception as e:
        # 与 Java 一致：索引创建失败不阻塞启动
        log.warning("1:1 唯一索引创建失败（不影响启动）：%s", e)


def _backfill_gate_reqrev(db: Database) -> None:
    import json as _json
    n = 0
    for row in db.query_all(
            "SELECT r.id, r.req_id, r.baseline_json FROM gate_round r JOIN requirement q ON q.id=r.req_id"):
        raw = str(row["baseline_json"] or "")
        try:
            b = _json.loads(raw or "{}")
        except _json.JSONDecodeError:
            continue
        if not isinstance(b, dict) or "reqRev" in b:
            continue
        b["reqRev"] = int(q_val) if (q_val := db.query_scalar(
            "SELECT content_rev FROM requirement WHERE id=?", (str(row["req_id"]),))) else 0
        db.execute("UPDATE gate_round SET baseline_json=? WHERE id=?",
                   (_json.dumps(b, ensure_ascii=False), int(row["id"])))
        n += 1
    if n:
        log.info("GR-3 基线 reqRev 存量回填：%d 轮", n)
