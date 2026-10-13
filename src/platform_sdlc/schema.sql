-- SDLC 平台建表脚本（IF NOT EXISTS 可重复执行）
-- v2.1 重建说明：原文件在架构升级事故中损坏，此版自 live SQLite（platform.db）.schema 提取生成，结构以运行库为权威；列注释从 git 历史与代码整理

CREATE TABLE IF NOT EXISTS requirement ( id TEXT PRIMARY KEY, name TEXT NOT NULL, iter TEXT DEFAULT '', source_type TEXT NOT NULL, source_url TEXT DEFAULT '', source_text TEXT DEFAULT '', status TEXT NOT NULL, submitted_by TEXT DEFAULT '', standard_doc TEXT DEFAULT '', created_at TEXT, synced_at TEXT, domains TEXT DEFAULT '', deleted_at TEXT DEFAULT '', risk_tier TEXT DEFAULT '', target_repo TEXT DEFAULT '', design_final_json TEXT DEFAULT '', target_repos TEXT DEFAULT '' );
CREATE TABLE IF NOT EXISTS clarify_item ( id INTEGER PRIMARY KEY AUTOINCREMENT, req_id TEXT NOT NULL, seq TEXT NOT NULL, level TEXT NOT NULL, theme TEXT DEFAULT '', question TEXT NOT NULL, ctx TEXT DEFAULT '', answer TEXT DEFAULT '', answered_at TEXT, quote TEXT DEFAULT '', anchor TEXT DEFAULT '', status TEXT DEFAULT '', note TEXT DEFAULT '', UNIQUE (req_id, seq) );
CREATE TABLE IF NOT EXISTS artifact ( req_id TEXT NOT NULL, type TEXT NOT NULL, markdown TEXT DEFAULT '', content_json TEXT DEFAULT '', version INTEGER NOT NULL DEFAULT 1, pushed_at TEXT, PRIMARY KEY (req_id, type) );
CREATE TABLE IF NOT EXISTS fr_point ( req_id TEXT NOT NULL, id TEXT NOT NULL, name TEXT DEFAULT '', source TEXT DEFAULT '', group_name TEXT DEFAULT '', module TEXT DEFAULT '', anchor TEXT DEFAULT '', PRIMARY KEY (req_id, id) );
CREATE TABLE IF NOT EXISTS media ( req_id TEXT NOT NULL, media_id TEXT NOT NULL, sha256 TEXT DEFAULT '', name TEXT DEFAULT '', mime TEXT DEFAULT '', bytes INTEGER NOT NULL DEFAULT 0, created_at TEXT, PRIMARY KEY (req_id, media_id) );
CREATE TABLE IF NOT EXISTS test_case ( req_id TEXT NOT NULL, id TEXT NOT NULL, title TEXT NOT NULL, points TEXT NOT NULL DEFAULT '[]', priority TEXT DEFAULT 'P2', pre TEXT DEFAULT '', steps TEXT DEFAULT '', expect TEXT DEFAULT '', owner TEXT DEFAULT '', tool TEXT DEFAULT '表单', status TEXT DEFAULT 'draft', version INTEGER NOT NULL DEFAULT 1, created_at TEXT, updated_at TEXT, verify_refs TEXT NOT NULL DEFAULT '[]', defect_refs TEXT NOT NULL DEFAULT '[]', case_no INTEGER, PRIMARY KEY (req_id, id) );
CREATE INDEX IF NOT EXISTS idx_case_req ON test_case(req_id);
CREATE TABLE IF NOT EXISTS case_comment ( id INTEGER PRIMARY KEY AUTOINCREMENT, req_id TEXT NOT NULL, case_id TEXT NOT NULL, by TEXT DEFAULT '', text TEXT NOT NULL, created_at TEXT );
CREATE INDEX IF NOT EXISTS idx_comment_case ON case_comment(req_id, case_id);
CREATE TABLE IF NOT EXISTS task ( id INTEGER PRIMARY KEY AUTOINCREMENT, req_id TEXT NOT NULL, type TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'queued', payload TEXT DEFAULT '', result TEXT DEFAULT '', runner TEXT DEFAULT '', created_by TEXT DEFAULT '', created_at TEXT, updated_at TEXT );
CREATE INDEX IF NOT EXISTS idx_task_req ON task(req_id);
CREATE TABLE IF NOT EXISTS draft_artifact ( req_id TEXT NOT NULL, type TEXT NOT NULL, md TEXT DEFAULT '', content_json TEXT DEFAULT '', task_id INTEGER, updated_at TEXT, PRIMARY KEY (req_id, type) );
CREATE TABLE IF NOT EXISTS seq_case_no ( id INTEGER PRIMARY KEY CHECK (id = 1), val INTEGER NOT NULL DEFAULT 0 );
-- v2.4 迭代实体（DESIGN-v2.4 D32）：迭代名即主键；requirement.iter 为指向迭代名的弱引用（不建 FK）；
-- 1:1 = 存活需求按 iter 唯一，服务层守卫 + 存量库检测干净后补 partial unique index（见 SchemaMigrator）
CREATE TABLE IF NOT EXISTS iteration ( name TEXT PRIMARY KEY, note TEXT DEFAULT '', created_by TEXT DEFAULT '', created_at TEXT );
-- v2.4 门禁域（DESIGN-v2.4 D35）：评审轮次权威两值（待裁决｜已放行）+ issue 裁决单元；旧轮只读归档
CREATE TABLE IF NOT EXISTS gate_round ( id INTEGER PRIMARY KEY AUTOINCREMENT, req_id TEXT NOT NULL, round_no INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'pending', checks_json TEXT DEFAULT '', baseline_json TEXT DEFAULT '', released_at TEXT, released_by TEXT DEFAULT '', created_by TEXT DEFAULT '', opened_at TEXT, UNIQUE (req_id, round_no) );
CREATE TABLE IF NOT EXISTS gate_issue ( id INTEGER PRIMARY KEY AUTOINCREMENT, round_id INTEGER NOT NULL, code TEXT NOT NULL, sev TEXT DEFAULT '', theme TEXT DEFAULT '', role TEXT DEFAULT '', scene TEXT DEFAULT '', quote TEXT DEFAULT '', loc TEXT DEFAULT '', basis TEXT DEFAULT '', div_flag INTEGER DEFAULT 0, tradeoff INTEGER DEFAULT 0, merged_unit INTEGER DEFAULT 0, filed INTEGER DEFAULT 0, verdict TEXT DEFAULT '', verdict_note TEXT DEFAULT '', verdict_by TEXT DEFAULT '', verdict_at TEXT, rev_location TEXT DEFAULT '', rev_status TEXT DEFAULT '' );
CREATE INDEX IF NOT EXISTS idx_gate_issue_round ON gate_issue(round_id);

-- 反向索引等读时聚合不落库；存量库补列逻辑见 SchemaMigrator

-- 批次6（AUDIT D2-1）：列表聚合高频谓词的支撑索引——澄清计数（answered IN 过滤）、
-- 任务状态扫描（claim/exists/expire）、门禁轮次读取（iteration 行级 gate_stage 子查询）。
-- 置于全部建表之后（executescript 顺序执行，索引先于建表会 no such table）。
CREATE INDEX IF NOT EXISTS idx_clarify_req_status ON clarify_item(req_id, status);
CREATE INDEX IF NOT EXISTS idx_task_status ON task(status);
CREATE INDEX IF NOT EXISTS idx_gate_round_req ON gate_round(req_id);
