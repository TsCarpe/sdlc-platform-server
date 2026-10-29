-- SDLC 平台建表脚本（MySQL 8，EXEC-PLAN-K8s 阶段 2）
-- 拆分器护栏（db.executescript）：注释一律整行 `--` 前缀；SQL 字符串字面量禁止含 ASCII `;`。
-- 幂等：CREATE TABLE IF NOT EXISTS 由语法兜底；索引为裸 CREATE INDEX，重复执行由适配层容忍 1061。
-- 类型规则：谓词列（WHERE 等值/!=''/ORDER BY）VARCHAR(n) DEFAULT ''（NULL 永不出现）；
--   内容列 MEDIUMTEXT DEFAULT ('')、短文本 TEXT DEFAULT ('')（MySQL TEXT 无字面量 DEFAULT，用表达式默认值，
--   兜住 INSERT 非全列供值的历史写法）；标识符/join/唯一键列统一 COLLATE utf8mb4_bin（等值与 UNIQUE
--   保持大小写敏感，对齐 SQLite BINARY）；纯时间列 DATETIME（适配层出口转字符串），双态标志列
--   （requirement.deleted_at、req_block.deleted：''/时间戳）保 VARCHAR(32)。
-- 存量迭代 1:1 唯一（uq_req_iter_alive，生成列方案）不在此：由 migrations._try_unique_req_iter_index
--   按「dup 检测→ALTER 补生成列→建唯一索引」守护（进 schema 会让存量 dup 库启动即 1062）。

CREATE TABLE IF NOT EXISTS requirement ( id VARCHAR(32) COLLATE utf8mb4_bin NOT NULL, name VARCHAR(512) DEFAULT '', iter VARCHAR(128) COLLATE utf8mb4_bin DEFAULT '', source_type VARCHAR(16) NOT NULL, source_url VARCHAR(2048) DEFAULT '', source_text MEDIUMTEXT DEFAULT (''), status VARCHAR(32) NOT NULL, submitted_by VARCHAR(255) DEFAULT '', standard_doc MEDIUMTEXT DEFAULT (''), created_at DATETIME NULL, synced_at DATETIME NULL, domains VARCHAR(1024) DEFAULT '', deleted_at VARCHAR(32) DEFAULT '', risk_tier VARCHAR(8) DEFAULT '', target_repo VARCHAR(2048) DEFAULT '', design_final_json MEDIUMTEXT DEFAULT (''), target_repos MEDIUMTEXT DEFAULT (''), content_rev BIGINT NOT NULL DEFAULT 0, PRIMARY KEY (id) ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
CREATE TABLE IF NOT EXISTS clarify_item ( id BIGINT AUTO_INCREMENT PRIMARY KEY, req_id VARCHAR(32) COLLATE utf8mb4_bin NOT NULL, seq VARCHAR(16) COLLATE utf8mb4_bin NOT NULL, level VARCHAR(8) NOT NULL, theme VARCHAR(255) DEFAULT '', question TEXT NOT NULL, ctx TEXT DEFAULT (''), answer TEXT DEFAULT (''), answered_at DATETIME NULL, quote TEXT DEFAULT (''), anchor VARCHAR(64) COLLATE utf8mb4_bin DEFAULT '', status VARCHAR(16) DEFAULT '', note TEXT DEFAULT (''), origin VARCHAR(16) DEFAULT '', src_comment_id BIGINT NULL, options TEXT DEFAULT (''), module VARCHAR(255) DEFAULT '', layer VARCHAR(16) DEFAULT '', UNIQUE KEY uq_clarify_req_seq (req_id, seq) ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
CREATE TABLE IF NOT EXISTS artifact ( req_id VARCHAR(32) COLLATE utf8mb4_bin NOT NULL, type VARCHAR(32) NOT NULL, markdown MEDIUMTEXT DEFAULT (''), content_json MEDIUMTEXT DEFAULT (''), version BIGINT NOT NULL DEFAULT 1, pushed_at DATETIME NULL, PRIMARY KEY (req_id, type) ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
CREATE TABLE IF NOT EXISTS fr_point ( req_id VARCHAR(32) COLLATE utf8mb4_bin NOT NULL, id VARCHAR(64) COLLATE utf8mb4_bin NOT NULL, name VARCHAR(512) DEFAULT '', source VARCHAR(32) DEFAULT '', group_name VARCHAR(255) DEFAULT '', module VARCHAR(255) DEFAULT '', anchor VARCHAR(64) COLLATE utf8mb4_bin DEFAULT '', PRIMARY KEY (req_id, id) ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
CREATE TABLE IF NOT EXISTS media ( req_id VARCHAR(32) COLLATE utf8mb4_bin NOT NULL, media_id VARCHAR(64) COLLATE utf8mb4_bin NOT NULL, sha256 VARCHAR(64) DEFAULT '', name VARCHAR(255) DEFAULT '', mime VARCHAR(128) DEFAULT '', bytes BIGINT NOT NULL DEFAULT 0, created_at DATETIME NULL, PRIMARY KEY (req_id, media_id) ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
CREATE TABLE IF NOT EXISTS test_case ( req_id VARCHAR(32) COLLATE utf8mb4_bin NOT NULL, id VARCHAR(64) COLLATE utf8mb4_bin NOT NULL, title VARCHAR(512) NOT NULL, points TEXT NOT NULL DEFAULT ('[]'), priority VARCHAR(8) DEFAULT 'P2', pre TEXT DEFAULT (''), steps TEXT DEFAULT (''), expect TEXT DEFAULT (''), owner VARCHAR(255) DEFAULT '', tool VARCHAR(64) DEFAULT '表单', status VARCHAR(16) DEFAULT 'draft', version BIGINT NOT NULL DEFAULT 1, created_at DATETIME NULL, updated_at DATETIME NULL, verify_refs TEXT NOT NULL DEFAULT ('[]'), defect_refs TEXT NOT NULL DEFAULT ('[]'), case_no BIGINT NULL, PRIMARY KEY (req_id, id) ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
CREATE INDEX idx_case_req ON test_case(req_id);
CREATE TABLE IF NOT EXISTS case_comment ( id BIGINT AUTO_INCREMENT PRIMARY KEY, req_id VARCHAR(32) COLLATE utf8mb4_bin NOT NULL, case_id VARCHAR(64) COLLATE utf8mb4_bin NOT NULL, `by` VARCHAR(255) DEFAULT '', text TEXT NOT NULL, created_at DATETIME NULL ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
-- `by` 为 MySQL 保留字，建表与 INSERT（comment_repo）统一反引号；SELECT * 返回列名不带反引号
CREATE INDEX idx_comment_case ON case_comment(req_id, case_id);
CREATE TABLE IF NOT EXISTS task ( id BIGINT AUTO_INCREMENT PRIMARY KEY, req_id VARCHAR(32) COLLATE utf8mb4_bin NOT NULL, type VARCHAR(32) NOT NULL, status VARCHAR(16) NOT NULL DEFAULT 'queued', payload MEDIUMTEXT DEFAULT (''), result MEDIUMTEXT DEFAULT (''), runner VARCHAR(255) DEFAULT '', attempt INT NOT NULL DEFAULT 0, created_by VARCHAR(255) DEFAULT '', created_at DATETIME NULL, updated_at DATETIME NULL ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
CREATE INDEX idx_task_req ON task(req_id);
CREATE TABLE IF NOT EXISTS draft_artifact ( req_id VARCHAR(32) COLLATE utf8mb4_bin NOT NULL, type VARCHAR(32) NOT NULL, md MEDIUMTEXT DEFAULT (''), content_json MEDIUMTEXT DEFAULT (''), task_id BIGINT NULL, updated_at DATETIME NULL, PRIMARY KEY (req_id, type) ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
CREATE TABLE IF NOT EXISTS seq_case_no ( id INT NOT NULL, val BIGINT NOT NULL DEFAULT 0, PRIMARY KEY (id), CHECK (id = 1) ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
-- 批次6（AUDIT D2-2）：迁移账本——一次性迁移步骤已应用即跳过（省去每次启动的空转全表扫描；
-- 判列补列/建表/索引本身幂等，不入账本）
CREATE TABLE IF NOT EXISTS schema_version ( name VARCHAR(190) COLLATE utf8mb4_bin NOT NULL, applied_at DATETIME NULL, PRIMARY KEY (name) ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
-- v2.4 迭代实体（DESIGN-v2.4 D32）：迭代名即主键；requirement.iter 为指向迭代名的弱引用（不建 FK）；
-- 1:1 = 存活需求按 iter 唯一，服务层守卫 + 存量库检测干净后补生成列唯一索引（见 SchemaMigrator）
CREATE TABLE IF NOT EXISTS iteration ( name VARCHAR(128) COLLATE utf8mb4_bin NOT NULL, note TEXT DEFAULT (''), created_by VARCHAR(255) DEFAULT '', created_at DATETIME NULL, PRIMARY KEY (name) ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
-- v2.4 门禁域（DESIGN-v2.4 D35）：评审轮次权威两值（待裁决｜已放行）+ issue 裁决单元；旧轮只读归档
CREATE TABLE IF NOT EXISTS gate_round ( id BIGINT AUTO_INCREMENT PRIMARY KEY, req_id VARCHAR(32) COLLATE utf8mb4_bin NOT NULL, round_no INT NOT NULL, status VARCHAR(16) NOT NULL DEFAULT 'pending', checks_json MEDIUMTEXT DEFAULT (''), baseline_json MEDIUMTEXT DEFAULT (''), review_mode VARCHAR(32) DEFAULT '', fanout INT NOT NULL DEFAULT 0, context_mode VARCHAR(32) DEFAULT '', released_at DATETIME NULL, released_by VARCHAR(255) DEFAULT '', created_by VARCHAR(255) DEFAULT '', opened_at DATETIME NULL, UNIQUE KEY uq_gate_round_req_round (req_id, round_no) ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
CREATE TABLE IF NOT EXISTS gate_issue ( id BIGINT AUTO_INCREMENT PRIMARY KEY, round_id BIGINT NOT NULL, code VARCHAR(64) NOT NULL, sev VARCHAR(8) DEFAULT '', theme VARCHAR(255) DEFAULT '', role VARCHAR(64) DEFAULT '', scene VARCHAR(255) DEFAULT '', quote TEXT DEFAULT (''), loc VARCHAR(255) DEFAULT '', basis TEXT DEFAULT (''), div_flag TINYINT DEFAULT 0, tradeoff TINYINT DEFAULT 0, merged_unit INT DEFAULT 0, filed TINYINT DEFAULT 0, verdict VARCHAR(16) DEFAULT '', verdict_note TEXT DEFAULT (''), verdict_by VARCHAR(255) DEFAULT '', verdict_at DATETIME NULL, rev_location VARCHAR(255) DEFAULT '', rev_status VARCHAR(16) DEFAULT '' ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
CREATE INDEX idx_gate_issue_round ON gate_issue(round_id);
-- v4.1 需求平台权威化（DESIGN-v4 §2.2）：原文块表——一行一块，锚点（块 ID）即主键；导入沿用飞书块 ID，
-- 平台编辑新发 blk-<ulid>（禁内容哈希——重复文本是常态）；派生字段（path/ordinal）读时推导不落库；
-- comment 不入块表（评论单一权威在 req_comment）；seq 间隔 1000，重排=事务内整体重排属常规操作
CREATE TABLE IF NOT EXISTS req_block (
  req_id VARCHAR(32) COLLATE utf8mb4_bin NOT NULL,
  block_id VARCHAR(64) COLLATE utf8mb4_bin NOT NULL,
  seq BIGINT NOT NULL,
  type VARCHAR(32) NOT NULL,
  text MEDIUMTEXT DEFAULT (''),
  attrs_json TEXT DEFAULT (''),
  version BIGINT NOT NULL DEFAULT 1,
  updated_at DATETIME NULL,
  updated_by VARCHAR(255) DEFAULT '',
  deleted VARCHAR(32) DEFAULT '',
  PRIMARY KEY (req_id, block_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
CREATE INDEX idx_req_block_req_seq ON req_block(req_id, seq);
-- v4.1 评论表（DESIGN-v4 §4.2）：块级（block_id 非空）/卡片级（空）双锚；origin 区分平台评论与飞书导入；
-- external_id = 飞书 comment-id，幂等重导的唯一定位键
CREATE TABLE IF NOT EXISTS req_comment (
  id BIGINT AUTO_INCREMENT PRIMARY KEY,
  req_id VARCHAR(32) COLLATE utf8mb4_bin NOT NULL,
  block_id VARCHAR(64) COLLATE utf8mb4_bin DEFAULT '',
  origin VARCHAR(16) DEFAULT 'platform',
  external_id VARCHAR(64) COLLATE utf8mb4_bin DEFAULT '',
  author VARCHAR(255) DEFAULT '',
  text TEXT NOT NULL,
  reply_to BIGINT NULL,
  resolved TINYINT DEFAULT 0,
  created_at DATETIME NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
CREATE INDEX idx_req_comment_req ON req_comment(req_id);
CREATE INDEX idx_req_comment_ext ON req_comment(req_id, external_id);

-- 反向索引等读时聚合不落库；存量库补列逻辑见 SchemaMigrator

-- 批次6（AUDIT D2-1）：列表聚合高频谓词的支撑索引——澄清计数（answered IN 过滤）、
-- 任务状态扫描（claim/exists/expire）、门禁轮次读取（iteration 行级 gate_stage 子查询）。
-- 置于全部建表之后（拆分器顺序执行，索引先于建表会 no such table）。
CREATE INDEX idx_clarify_req_status ON clarify_item(req_id, status);
CREATE INDEX idx_task_status ON task(status);
CREATE INDEX idx_gate_round_req ON gate_round(req_id);
-- v4.11 变更单（DESIGN-v4 §5.2）：已发布需求原文变更的结构化载体；draft 可删，confirmed 不作废（放弃走 §5.6 逆向回滚）
CREATE TABLE IF NOT EXISTS req_change ( id BIGINT AUTO_INCREMENT PRIMARY KEY, req_id VARCHAR(32) COLLATE utf8mb4_bin NOT NULL, kind VARCHAR(16) DEFAULT 'change', title VARCHAR(512) DEFAULT '', detail TEXT DEFAULT (''), proposed_by VARCHAR(255) DEFAULT '', confirmed_by VARCHAR(255) DEFAULT '', status VARCHAR(16) NOT NULL DEFAULT 'draft', impact_json MEDIUMTEXT DEFAULT (''), blocks_json MEDIUMTEXT DEFAULT (''), snapshot_json MEDIUMTEXT DEFAULT (''), aborted_by VARCHAR(255) DEFAULT '', aborted_at DATETIME NULL, merged_at DATETIME NULL, created_at DATETIME NULL, confirmed_at DATETIME NULL ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
CREATE INDEX idx_req_change_req ON req_change(req_id);
CREATE TABLE IF NOT EXISTS req_change_item ( id BIGINT AUTO_INCREMENT PRIMARY KEY, change_id BIGINT NOT NULL, block_id VARCHAR(64) COLLATE utf8mb4_bin NOT NULL, op VARCHAR(8) NOT NULL, old_text MEDIUMTEXT DEFAULT (''), new_text MEDIUMTEXT DEFAULT (''), old_attrs TEXT DEFAULT (''), new_attrs TEXT DEFAULT ('') ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
CREATE INDEX idx_req_change_item_change ON req_change_item(change_id);
-- 助理会话（M3 并入主库，原独立 assistant.db 退役）：session_id+idx 定位消息序，data=JSON 串；
-- 自增 id 承担 SQLite 时期 rowid 的会话先序职责（list_session_ids ORDER BY MIN(id)）
CREATE TABLE IF NOT EXISTS chat_items (
  id BIGINT AUTO_INCREMENT PRIMARY KEY,
  session_id VARCHAR(64) COLLATE utf8mb4_bin NOT NULL,
  idx INT NOT NULL,
  data MEDIUMTEXT DEFAULT (''),
  UNIQUE KEY uq_chat_session_idx (session_id, idx)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
