# SDLC 产物协作平台 · 开发计划（v1）

> 2026-09-18 制定。上游共识：grilling 访谈定稿 + demo 验收（platform-demo/index.html，含真实数据 data.js）。
> 本文档是 v1 的唯一范围依据；改范围先改这里。

---

## 0. 共识锚点（钉死，防蔓延）

**要解决的三个断层**：空间（产物在个人本地）、回环（PM 答复人肉誊回）、权威（团队没有共享登记处）。

**v1 范围 = 两个公共产物域**：

| 域 | 流程 | 明确不做 |
|---|---|---|
| 需求线 | PM 双通道提交（飞书链接 / 粘贴文本）→ skill 本地质检 → 三件套推平台 → **PM 平台内逐条答复** → skill 拉回答答回写本地 → 重算 digest → 标准需求文档发布 | 通知自动化、事件驱动触发、登录、富文本编辑器、需求生命周期状态（开发中/已测试） |
| 用例线 | 登记表：skill API 写入 + 表单提交（非 skill 用户），关联需求点软校验，审核态 待审核→已定稿 | 执行结果、缺陷、执行证据（截图/日志）上平台 |

**架构铁律**：
1. 本地 md = skill 工作权威（六个现有 skill 内部消费链零改动）；平台 = 共享权威与交互面。
2. 桥 = 平台 REST API + 新建独立 skill `sdlc-sync`（push/pull，全手动触发）。
3. 编辑权随工作区：sdlc-test 推送的用例对他人只读+评论；表单提交的用例提交人可编辑。
4. 状态机由 skill 同步写入，其他人只读：需求 `已提交→质检中→待澄清→已发布`；用例 `待审核→已定稿`。
5. 无登录（内网约定，防误操作不防故意）；身份为手填字段。
6. 关卡纪律继承：推送/发布均为人触发，自动同步不存在。

**技术栈**（已定）：Spring Boot + Vue，单服务单库，单维护者——一切从简压倒技术偏好。

---

## 1. 架构与技术选型

```
┌─────────────┐   REST/JSON    ┌──────────────────┐
│  sdlc-sync   │ ─────────────→ │  Spring Boot 单体 │──→ SQLite（单文件）
│  (skill,     │ ←───────────── │  + 静态托管 Vue    │
│   本地 agent) │   push / pull  │  (SPA)            │
└─────────────┘                └──────────────────┘
        ▲                               ▲
        │ 读取本地 sdlc/<需求名>/        │ 浏览器（PM/测试/开发，
        │ intake/*.md, test/cases.md    │ 内网，无登录）
```

- **后端**：Spring Boot 3 Web + MyBatis（或团队顺手的持久层），无微服务、无消息队列、无缓存层。
- **数据库**：SQLite 单文件起步（备份=拷文件）；表设计不使用 SQLite 专属特性，保留切 MySQL 的余地。量级预估：需求几十、用例几百，SQLite 富余。
- **前端**：Vite + Vue 3 SFC。**直接移植 demo**（样式、交互、组件划分已验收），markdown 渲染用 marked + DOMPurify。不引 UI 组件库。
- **部署**：内网任意一台常开机器，`java -jar` + Nginx 静态托管（或 Spring Boot 直接托管 dist），一键启停脚本。M4 才做，前四个里程碑本机跑。

---

## 2. 数据模型（v1 全部表）

```sql
-- 需求卡片
CREATE TABLE requirement (
  id            TEXT PRIMARY KEY,          -- REQ-2026-xxx，平台分配
  name          TEXT NOT NULL,
  iter          TEXT DEFAULT '',           -- 迭代，自由文本，如 edu-region
  source_type   TEXT NOT NULL,             -- feishu | text
  source_url    TEXT DEFAULT '',
  source_text   TEXT DEFAULT '',
  status        TEXT NOT NULL,             -- submitted|reviewing|clarifying|published
  submitted_by  TEXT DEFAULT '',
  standard_doc  TEXT DEFAULT '',           -- 发布时合成/推送的标准文档 markdown
  created_at    TEXT, synced_at TEXT
);

-- 澄清问题（pm-checklist 的平台形态，即交互答复单元）
CREATE TABLE clarify_item (
  id        INTEGER PRIMARY KEY AUTOINCREMENT,
  req_id    TEXT REFERENCES requirement(id),
  seq       TEXT NOT NULL,                -- P01 / P19 等编号（skill 推送时给定）
  level     TEXT NOT NULL,                -- red|yellow|blue
  theme     TEXT DEFAULT '',              -- 所属主题，如「修改比赛的规则簇」
  question  TEXT NOT NULL,
  ctx       TEXT DEFAULT '',              -- 原文定位
  answer    TEXT DEFAULT '',              -- PM 答复
  answered_at TEXT
);

-- 产物快照（digest / audit 原文 + 版本，只留最新版即可，旧版在本地 git）
CREATE TABLE artifact (
  req_id    TEXT REFERENCES requirement(id),
  type      TEXT NOT NULL,                -- digest | audit | standard
  markdown  TEXT NOT NULL,
  version   INTEGER NOT NULL DEFAULT 1,
  pushed_at TEXT,
  PRIMARY KEY (req_id, type)
);

-- 需求点注册表（追溯外键 + 表单软校验数据源）
CREATE TABLE fr_point (
  id      TEXT PRIMARY KEY,               -- 需求.FR-01（带命名空间）
  req_id  TEXT REFERENCES requirement(id),
  name    TEXT DEFAULT ''
);

-- 用例登记
CREATE TABLE test_case (
  id        TEXT NOT NULL,                -- TC-01（来源方自己的编号体系）
  req_id    TEXT REFERENCES requirement(id),
  title     TEXT NOT NULL,
  points    TEXT NOT NULL DEFAULT '[]',   -- JSON 数组，如 ["需求.FR-01"]
  priority  TEXT DEFAULT 'P2',
  pre       TEXT DEFAULT '',
  steps     TEXT DEFAULT '',
  expect    TEXT DEFAULT '',
  owner     TEXT DEFAULT '',              -- 来源人（手填/skill 推送时给）
  tool      TEXT DEFAULT '表单',          -- sdlc-test | 表单
  status    TEXT DEFAULT 'draft',         -- draft 待审核 | reviewed 已定稿
  version   INTEGER NOT NULL DEFAULT 1,   -- upsert 乐观锁
  created_at TEXT, updated_at TEXT,
  PRIMARY KEY (req_id, id)
);

-- 用例评论
CREATE TABLE case_comment (
  id      INTEGER PRIMARY KEY AUTOINCREMENT,
  req_id  TEXT, case_id TEXT,
  by      TEXT DEFAULT '', text TEXT NOT NULL, created_at TEXT
);
```

只读规则 = `tool='sdlc-test'` 时 UI 隐藏编辑（软约定）。需求点软校验 = 提交时查 `fr_point`，未命中则提示不拦截。

---

## 3. API 契约（v1 全部端点）

约定：`/api` 前缀，JSON；无鉴权（内网）；写接口 accept `X-Client: sdlc-sync|web` 仅作日志区分。

| 方法/路径 | 调用方 | 作用 |
|---|---|---|
| `POST /api/requirements` | web | PM/用户建卡片：`{name, iter, source_type, source_url?, source_text?, submitted_by}` → 状态 submitted |
| `GET /api/requirements?iter=&status=` | web | 列表（含澄清进度、用例计数） |
| `GET /api/requirements/{id}` | 双方 | 详情：卡片 + clarify_items + artifacts 元信息 |
| `POST /api/requirements/{id}/artifacts` | **sync** | 推质检产物：`{digest_md, audit_md, items:[{seq,level,theme,question,ctx,answer?}], fr_points:[{id,name}]}`，创建或全量替换 clarify_item，状态→clarifying；重复推送 = 覆盖（version+1） |
| `GET /api/requirements/{id}/answers` | **sync** | 拉答复：`{items:[{seq, question, answer, answered_at}]}`（含未答，sync 自行判断） |
| `POST /api/requirements/{id}/publish` | **sync** | `{standard_md}` → 状态 published |
| `POST /api/requirements/{id}/status` | **sync** | 中间态修正：`{status}`（仅允许 reviewing） |
| `GET /api/fr-points?req_id=` | web | 软校验数据源 |
| `POST /api/cases` | **sync** | 批量 upsert：`{items:[{id,title,points,priority,pre,steps,expect,owner,status,expected_version?}]}`，命中 version 不符返回 409 + 服务端当前值 |
| `POST /api/cases/form` | web | 表单提交（无版本，新建 draft） |
| `PATCH /api/cases/{reqId}/{caseId}` | web | 编辑（仅表单来源；带 expected_version） |
| `POST /api/cases/{reqId}/{caseId}/comments` | web | 评论 |
| `GET /api/cases?iter=&req_id=&status=&tool=&q=` | web | 列表筛选 |

**契约原则**：推送接口全部是"全量替换"语义（平台是共享视图，不是增量数据库）；skill 侧永远以本地文件为源生成全量 payload。

---

## 4. sdlc-sync skill（新建，落在 roadmap sdlc-doc 的位置）

形态：`SKILL.md` 指导 agent 读本地 md → 组装 JSON → curl 调平台 API。**不写独立解析器**，结构化由 agent 完成；契约中给出 payload JSON Schema 作为 skill 的 references 文件。

| 子命令 | 读（本地） | 写（平台） | 副作用（本地） |
|---|---|---|---|
| `push-artifacts <req-id>` | intake/digest、audit、pm-checklist 三件套 | artifacts + 全量 clarify_items + fr_points，状态→clarifying | 记录 req-id 到需求目录 `sdlc/.platform` |
| `pull-answers <req-id>` | — | GET answers | **回写本地** pm-checklist 答复列 + audit 处理状态列；提示用户重跑 sdlc-intent 更新 digest |
| `publish <req-id>` | 重算后的 digest + 澄清结论 | publish（标准文档 md），状态→published | 本地归档 standard-\<日期\>.md |
| `push-cases <req-id>` | test/cases.md（总览+明细+追踪表） | cases 批量 upsert | cases.md 头部记录"已同步至平台 @版本" |

配套：
- `submit-requirement`：平台建卡后，把卡片 req-id 与飞书链接交给用户（流程入口说明写进 skill）。
- **eval**：用真实 `区域教学技能比赛` 四个文件做解析评测（31 问 0 漏、42 例 0 漏、points 与追踪表一致）。

---

## 5. 里程碑（每期交付可用增量，验收以真实数据为准）

### M0 建仓与脚手架（0.5~1 天）
- 新建独立仓库（建议 `sdlc-platform/`，与 skills 仓分离）；Spring Boot + Vite/Vue3 骨架；SQLite 初始化；建表 SQL 落库；`make dev` 一键起后端+前端。
- demo 仓库的 data.js / index.html 迁入新仓作为前端移植底稿。
- **完成定义**：本机两条命令起服务，空页面可访问。

### M1 需求线只读闭环（3~4 天）
- 后端：requirement / clarify_item / artifact / fr_point 表 + API（建卡、列表、详情、artifacts 推送、answers 拉取）。
- 前端：需求列表 + 详情四 tab（原始需求/Digest/体检/澄清清单），移植 demo；markdown 渲染。
- sync 原型：以 curl 脚本形态先跑通 push-artifacts（skill 化在 M4）。
- **完成定义**：把真实的「区域教学技能比赛」三件套推上平台，页面呈现 digest 96 FR / 体检 31 条 / 澄清 16/31，与 demo 一致。

### M2 澄清闭环——平台的灵魂（2~3 天）
- 前端：PM 答复编辑（澄清中全程可改）、进度条、发布态只读切换。
- 后端 + sync：publish 流程；标准需求文档渲染 tab。
- 状态机流转接通（submitted→reviewing→clarifying→published）。
- **完成定义**：真实需求的 15 条未答在页面上作答 → pull-answers 回写本地 md → 重跑 intent → publish，全链路走通（可先复制需求演练，不动真数据）。

### M3 用例线（2~3 天）
- 后端：test_case / case_comment 表 + upsert / form / comments / list API + 软校验。
- 前端：用例登记表 + 四维筛选 + 详情弹层（只读/编辑双形态）+ 表单提交，移植 demo。
- sync：push-cases（解析 cases.md）。
- **完成定义**：真实 42 条用例上平台且需求点关联正确；TC-17 只读+评论可用；表单提交一条新用例触发软校验。

### M4 硬化与上线（2 天）
- 迭代筛选、统计卡、空态、404/错误处理、版本冲突 409 行为、SQLite 备份脚本（cron 拷文件）。
- sdlc-sync 从 curl 脚本转正为 skill（SKILL.md + references/payload-schema.md + eval）。
- 内网部署（申请机器、一键脚本、访问地址广播）。
- **完成定义**：下一个真实新需求从提交→质检→澄清→发布全流程在平台上走完，PM 在页面真实答复 ≥1 轮；42 条用例 + 新用例在平台可查。

**总量级**：约 2~3 周（单人 + AI 辅助）。M1 结束即可向团队演示，M2 结束平台已"有用"，M3 结束"完整"，M4 后进入试运行。

---

## 6. 风险与对策

| 风险 | 对策 |
|---|---|
| **PM 采纳率**（最大风险：PM 不在平台上答复，平台退化成文件柜） | M2 完成后第一时间拉 PM 走一遍真实答复；答复入口做到"点开链接即答"零学习；试运行期你手动当人肉通知（发链接），把省下的誊回时间让 PM 可感知 |
| 无鉴权 API 被误写 | upsert 带版本号防覆盖；只读靠 UI 约定；如出事故，加固定 token 是一行配置（预留） |
| 飞书文档权限：PM 文档未开组织内可读 → lark-cli 拉取失败 | 提交卡片时表单文案提示"请将文档设为组织内可阅读"；skill 拉取失败时明确报权限而非重试 |
| agent 解析 cases.md 出错（结构复杂：追踪表/明细/总览三处冗余） | 以"用例总览"为登记源、明细补 steps/expect、追踪表反向校验 points；eval 用真实文件钉住；解析结果推平台前让用户过目一次 |
| 单维护者病假/离职，平台成孤儿 | 数据全部可导出（SQLite 单文件 + 全部 markdown 可再生成）；sdlc-sync pull 可反向导出平台数据到本地 md（M4 顺手实现 dump 命令） |
| 范围蔓延（通知/缺陷/执行态诱惑） | 第 0 节不做清单；v2 提案走本文档追加，不动代码先行 |

---

## 7. v2 钩子（现在不做，设计留缝）

1. 飞书群 webhook 通知（有新答复/状态流转时 @ 相关人）——表结构已留 synced_at/status，加通知表即可。
2. 缺陷与执行结果上链——等 AI 测试集中化；用例表 version 机制可直接复用。
3. 事件驱动自动质检（机器人监听需求群）。
4. 登录（飞书 OAuth）与真权限——无登录决策被现实打脸时再上，身份字段已存在。
5. 平台内富文本需求编辑（PM 直接在平台写需求）。
6. dump/reverse-sync 增强：平台表单用例回捞本地。

---

## 附：与现有 skills 仓的边界

- 本平台与 `claude-sdlc-skills` 仓**代码级零耦合**：skills 不感知平台，sdlc-sync 是唯一桥。
- `claude-sdlc-skills` 仓的变化仅两处：README Roadmap 里 sdlc-doc 条目改为指向 sdlc-sync；workflow 文档补一步"产物同步平台（可选）"。
- demo（platform-demo/）保留为产品原型参照，正式前端在新仓重写后归档。

---

## 附：实施记录（2026-09-18 夜间开发）

- **运行时**：本机仅 JDK 8 → Spring Boot 2.7.18（Java 8 兼容末版），未安装新 JDK。
- **持久层**：JdbcTemplate 手写仓库（计划中的"MyBatis 或顺手方案"），SQLite 单文件，Hikari 单连接。
- **契约细化**（相对第 3 节表格）：
  1. `POST /artifacts` payload 为结构化 digest/audit（summary/roles/features/计数/items）+ 可选原文 md，而非纯 md——平台渲染结构化视图，原文折叠展示。
  2. 新增 `PUT /api/requirements/{id}/answers/{seq}`（前端作答端点，第 3 节表格漏列）。
  3. 重推产物按 seq 保留平台已答内容（防覆盖 PM 人工作答）。
- **验收结果**：sync-real 15/15 PASS；verify-m2 8/8 PASS；浏览器目检通过（列表/详情/澄清作答/用例只读+评论/表单 API）。
- **联调数据清理**：验证用答复与 WEB 用例已清空/删除；真实需求 REQ-2026-001 保持 16/31 真实状态。
