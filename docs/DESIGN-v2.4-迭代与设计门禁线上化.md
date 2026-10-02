# DESIGN-v2.4 · 迭代与设计门禁线上化

> 状态：已实施（批次0–5 全部落地；每批次 = 编译 + 新旧 verify 全绿 + checklist 自检 + 一次 commit）
> 交互基准：`docs/prototype/design-gate-demo.html`（v2，迭代×需求 1:1 全流程工作台）+ 同目录评审报告
> 方法论权威：claude-sdlc-skills v0.16.0（payload 契约 v1.4，`skills/sdlc-sync/references/payload-schema.md`）
> 决策编号续接 DESIGN-v2.3（D31），本文从 **D32** 起。

## 0. 背景与目标

v2.0–v2.3 已把「需求线」全流程线上化（质检→澄清→发布→用例登记）。本版补齐后半程两条线与迭代维度：

1. **迭代域**：迭代成为一等实体（demo 口径：迭代×需求 **1:1**，平台首页 = 迭代列表）；支撑「新建迭代 → 空迭代 → 页内提交需求 → 全生命周期」主链。
2. **设计 lane**：发布后的技术设计生成/定稿线上化（target_repo 存量勘察配置、design_gen/design_rev 任务、C 级轻量跳过）。
3. **门禁域**：评审轮次、issue 裁决、放行与变更重审的平台化（GR-2/3/4 机械校验平台侧落地）。
4. **前端**：迭代列表首页 + 迭代工作台（8 tab）对齐 demo 交互，零新依赖。

## 1. 兼容红线（全批次回归门槛）

| # | 红线 | 依据 |
|---|---|---|
| R1 | 需求状态机不动：`submitted→reviewing→clarifying→published`（终态锁）。设计/门禁为**独立域**，不扩 requirement.status —— 同时解除评审报告 §8「published 终态锁 × GR-3 放行后重开」张力 | payload v1.4；GR-1 输入冻结 |
| R2 | skill 权威两值落库：设计「已定稿｜轻量跳过」、门禁「待裁决｜已放行(日期)」；六态 UI（running/failed/blocked/summary/deciding 等）由平台推导，页面加「平台化推导」标注 | sdlc-gate SKILL；demo :1903 口径 |
| R3 | payload v1.4 全兼容：建卡 iter 仍为自由文本，iter 不存在时平台**自动建迭代**（D33）；verify-m2/guard/m3/m4/m5/m6/delete 全绿 | 契约双向红线 |
| R4 | 执行侧只写草稿（put_draft），正式产物只能人采纳（approve/publish）；AgentPlatformTools 工具面红线不放松 | DESIGN-v2 守卫继承 |
| R5 | 前端零新依赖；markdown 过 DOMPurify；GR-5 跳转失败诚实 toast | CLAUDE.md 硬约束 |
| R6 | AI 生成质量不在本轮验收范围：verify 脚本按 verify-m5/m6 口径模拟任务完成（POST done + 草稿），真实 agent 联调列为遗留 | verify-all 协议 |

## 2. 决策表

### D32 迭代实体化（批次1）

- 新表 `iteration`：`name TEXT PRIMARY KEY`（迭代名即主键，demo 用 `v3.7.2` 风格）、`note`、`created_by`、`created_at`。
- `requirement.iter` **保留原列**作为指向迭代名的弱引用（不建 FK：SQLite 迁移与 skill 契约均友好）。
- **1:1 语义 = 存活需求按 iter 唯一**：
  - 服务层守卫：建卡时 iter 非空且已有存活需求绑定 → 409（提示迭代与需求为 1:1）。
  - 存量库：SchemaMigrator 检测无重复后建 partial unique index `uq_req_iter_alive ON requirement(iter) WHERE deleted_at='' AND iter!=''`（数据库层兜底）；检测到脏数据只告警跳过（诚实降级，不阻塞启动）。
  - `iter=''`（未入迭代）不参与 1:1；软删需求自动释放迭代名（守卫只数存活行）。
- 建卡 iter 在迭代表中不存在 → 自动建迭代行（R3 契约兼容；`@Transactional` 保证守卫+建行+建卡原子）。
- 平台侧约束：iter 建卡时 trim、非空 ≤64 字（新约束，skill 侧 iter 本就短代号，风险可忽略）；迭代名同口径。

### D33 迭代聚合接口（批次1）

- `GET /api/iterations`：平铺行（迭代元数据 + 绑定需求摘要）。绑定需求用相关子查询取**最新一条存活行**（脏库多行时保证每迭代一行，确定性排序 `created_at DESC, id DESC`）。
- 需求摘要字段：reqId/reqName/reqStatus/riskTier/totalQ/answeredQ/caseCount（复用 RequirementListRow 聚合口径）。
- **生命周期节点条 / 六态推导不落库、不在后端聚合**——前端以 detail 数据单一来源推导（demo `nodesOf/actOf` 口径移植），后端只供原始态。
- 空迭代可删（无存活绑定需求）；有绑定 → 409。迭代行不参与软删（删除 = 物理删，行内无产物）。

### D34 设计 lane（批次2）

- requirement 加列：`target_repo TEXT DEFAULT ''`（存量勘察目标仓，设计生成前置）、`design_final_json TEXT DEFAULT ''`（定稿留痕 JSON：by/at/ver、轻量跳过声明 by/at、tier 提案快照——免建表，读面为 design 域专用）。
- 草稿/产物类型 +`design`：`put_draft(design)` → 人**定稿采纳**（approve）→ `artifact(type=design)`，version 递增承接落改修订（GR-4 落改闭环的载体）。
- approve 守卫：design 草稿需 target_repo 已配置或 C 级（自查档不勘察）；published 前置（设计 lane 属发布后流程，需求未发布 → 409）。
- C 级轻量跳过：`POST /{id}/design/skip` 留痕声明（by/at 落 design_final_json），声明后 design_gen 拒绝 409；门禁以自查档执行。
- 未配 target_repo（A/B 级）→ design_gen 建任务 400（demo 守卫口径）。
- design_gen / design_rev 任务 + prompt（A=全量骨架/B=mini 按 tier 路由，C=不产文档）；selfCheck：design 草稿非空且含 §0–§8 标头。
- 发布编排钩子**不自动排 design_gen**（需先配仓+人工触发，demo 口径；case_gen 自动化保持不变）。

### D35 门禁域（批次3）

- 新表 `gate_round`：`id INTEGER PK`、`req_id`、`round_no`、`status`（权威两值：`待裁决｜已放行`+`released_at/released_by`）、`checks_json`（开闸三条件快照）、`baseline_json`（design md 内容指纹 + cases max(updated_at)，GR-3 基线）、`opened_at/created_by`。`UNIQUE(req_id, round_no)`。
- 新表 `gate_issue`：`round_id FK 语义`、`code`（D/A/S/T 前缀）、`sev(high|mid|low)`、`theme`、`role`、`scene`（白话）、`quote`、`loc`、`basis(hard|soft)`、`div(分歧)`、`tradeoff(权衡)`、`merged_unit`（RECONCILE 裁决单元号）、`filed(备案)`、`verdict(落改｜驳回｜存疑)`、`verdict_note`、`verdict_by/at`、`rev_location(落点)`、`rev_status(待执行｜已执行)`。
- **开闸**（GR-2 平台化，UI 标注）：`POST /{id}/gate/open` 机械校验三条件——①设计已定稿∨轻量跳过 ②用例≥1 且全部定稿(reviewed) ③FR 覆盖（RTM uncovered 为空）；不过 → 409 带逐条明细；通过 → 建轮（r1 起，已放行后再开 = r2 语义走 GR-3 reopen）+ 排 `gate_review` 任务（tier 分档 prompt：A=4 角色扇出/B=2/C=自查）。
- **issues 入库**：gate_review 产出 `put_draft(gate_issues)`（md=issues 文件 + contentJson=结构化 issues 数组）→ 人确认 `POST gate/rounds/{rid}/ingest`：解析入 gate_issue，md×json 计数一致性机械校验（check_trace 检查②同源口径），不一致 409。
- **裁决**：`POST gate/issues/{iid}/verdict`——落改（必填落点；可选触发 design_rev 任务生成修订草稿）/ 驳回（必填理由，落库沉淀误报模式，导出为遗留）/ 存疑（必填去向）。裁决单元 = merged_unit 分组（人裁决的是单元不是发现数，同单元批量落 verdict）。
- **放行**（GR-4 平台化）：`POST gate/rounds/{rid}/release`——全部裁决单元已裁决（备案除外）∧ 落改条目落点非空且已执行 ∧ 机械校验（FR 覆盖、引用可达抽查、计数一致）全绿 → status=已放行 + released_at/by + **关卡互认**（该需求全部 sdlc-test 用例置 reviewed + 确认人留痕，skill 口径「gate 放行视同关卡1 通过」）。任一不满足 → 409 带明细。
- **变更重审**（GR-3）：`GET /{id}/gate/change-status`——仅「定稿后未开审 / 已放行后」两窗口计算变更（design 终稿 md 比对基线 + cases updated_at>基线）；`POST /{id}/gate/reopen` 人确认开新一轮（round_no+1），旧轮数据只读保留（归档）。
- gate_phase 六态（none|blocked|running|summary|deciding|released）为读面推导：无轮=none；待评审任务活跃=running/blocked（三条件未过）；有轮有待裁决 issues=summary/deciding；已放行=released。落库仍只两值（R2）。

### D36 任务与草稿类型扩容（批次2/3）

- 任务类型 whitelist：+`design_gen`、`design_rev`、`gate_review`；草稿类型 whitelist：+`design`、`gate_issues`。
- 双执行通道同步：AgentScopeAgentExecutor（sysPrompt/goal/速查卡）+ runner.mjs `promptFor` 分支；两通道 prompt 均指向 skills 仓对应 skill（sdlc-design / sdlc-gate），红线不变：只允许 put_draft + upload_media。
- 建任务守卫继承：published 才可建 design_gen/design_rev/gate_review（前置锁 = R1 终态锁的域内表达）；countActive 去重沿用。

### D39 多目标代码仓（2026-09-30 重设计）

- 存量勘察的输入从「单个 target_repo 自由文本」升级为**多仓清单** `requirement.target_repos`（JSON 数组列，惯例同 test_case.points；读写整体全量替换，无行级操作，不建子表）。
- 条目结构 `{role, kind, ref, branch}`：**role** = 角色标签（前端/后端/…自由文本 ≤20，可空）；**kind** = `local`（本地路径）| `github`（远程仓）；**ref** = 本地绝对路径或 https 仓址（≤300）；**branch** 可选（≤100）。上限 8 条。
- 兼容：旧 `target_repo` 列保留为**首仓镜像**（读面 targetRepo 与执行通道降级兜底仍在），SchemaMigrator 把存量单仓值回填为单条目（kind 按前缀 http 推断 github，否则 local）；新读写一律走 target_repos。
- API：`PUT /api/requirements/{id}/target-repos`（body `{repos:[…]}`，全量替换）取代原 PATCH /target-repo；gen 守卫改为「至少 1 条仓」。
- design_gen 提示词逐仓枚举（角色+类型+地址+分支）；勘察能力仍受执行通道约束（进程内 agent 读不到本地仓时如实降级），github 远程仓的自动勘察通道列遗留 §6。

### D37 前端信息架构（批次4）

- home = 迭代列表（`#/iters`，`IterList.vue`）：迭代行 = 迭代名 + 需求 1:1 卡 + 七节点 node-strip（质检→澄清→发布→设计→用例→门禁→放行）+ 当前 blocker 徽章；空迭代行内嵌提交卡（demo 口径：提交入口在迭代详情/空迭代页内）。
- 工作台 = `ReqDetail` 升级 8 tab：origin/digest/audit/clarify/std + cases 内嵌（req 过滤的用例表） + design + gate；nextAction 全链主按钮（提交→质检→澄清→发布→配仓→生成设计→定稿→开闸→裁决→放行→r2）无空窗。
- 侧边栏：迭代（首页）/需求（保留为跨迭代二级视图，D38）/用例。
- trace.js 扩 `page:'iters'` 与 design/gate anchor kind；六态推导集中在单一模块（`lifecycle.js`），列表行与工作台共用。
- CSS：node-strip/gate-check/role-card/recon/issue 等从 demo 移植，token 对齐 style.css。

### D38 兼容与迁移（批次1/5）

- 旧需求列表页（#/reqs）与 `GET /api/requirements?iter=` 过滤保留不动。
- 存量库迁移（SchemaMigrator，幂等）：建 iteration 表 → 回填 `INSERT INTO iteration SELECT DISTINCT iter`（存活、非空）→ 重复检测通过则建 partial unique index。
- 演示/真实数据不动；verify 脚本一律自建夹具并清理。

## 3. 表结构（DDL 摘要）

```sql
CREATE TABLE IF NOT EXISTS iteration (
  name TEXT PRIMARY KEY, note TEXT DEFAULT '', created_by TEXT DEFAULT '', created_at TEXT );

-- 批次2：requirement ADD COLUMN target_repo TEXT DEFAULT '' / design_final_json TEXT DEFAULT ''
-- 批次3：
CREATE TABLE IF NOT EXISTS gate_round (
  id INTEGER PRIMARY KEY AUTOINCREMENT, req_id TEXT NOT NULL, round_no INTEGER NOT NULL,
  status TEXT NOT NULL DEFAULT '待裁决', checks_json TEXT DEFAULT '', baseline_json TEXT DEFAULT '',
  released_at TEXT, released_by TEXT DEFAULT '', created_by TEXT DEFAULT '', opened_at TEXT,
  UNIQUE (req_id, round_no) );
CREATE TABLE IF NOT EXISTS gate_issue (
  id INTEGER PRIMARY KEY AUTOINCREMENT, round_id INTEGER NOT NULL, code TEXT NOT NULL,
  sev TEXT DEFAULT '', theme TEXT DEFAULT '', role TEXT DEFAULT '', scene TEXT DEFAULT '',
  quote TEXT DEFAULT '', loc TEXT DEFAULT '', basis TEXT DEFAULT '', div_flag INTEGER DEFAULT 0,
  tradeoff INTEGER DEFAULT 0, merged_unit INTEGER DEFAULT 0, filed INTEGER DEFAULT 0,
  verdict TEXT DEFAULT '', verdict_note TEXT DEFAULT '', verdict_by TEXT DEFAULT '', verdict_at TEXT,
  rev_location TEXT DEFAULT '', rev_status TEXT DEFAULT '' );
CREATE INDEX IF NOT EXISTS idx_gate_issue_round ON gate_issue(round_id);
```

## 4. API 契约（新增端点）

| 端点 | 方法 | 说明 | 批次 |
|---|---|---|---|
| `/api/iterations` | GET | 迭代列表（平铺聚合行，绑定最新存活需求摘要） | 1 |
| `/api/iterations` | POST | 建迭代 `{name,note?,createdBy?}`；重名 409 | 1 |
| `/api/iterations/detail?name=` | GET | 迭代详情（含绑定需求摘要；空迭代 reqId=null）。迭代名可含 `/`，走 query 参（路径段被容器拒 %2F，2026-09-30 回归修复） | 1 |
| `/api/iterations/detail?name=` | DELETE | 删空迭代；有绑定 409 | 1 |
| `/api/requirements/{id}/target-repos` | PUT | 多目标仓全量替换 `{repos:[{role,kind,ref,branch}]}`（D39，取代原 PATCH target-repo） | 2 |
| `/api/requirements/{id}/design/skip` | POST | C 级轻量跳过声明（留痕） | 2 |
| `/api/requirements/{id}/design` | GET | 设计域读面（阶段/草稿/终稿/tier 提案/GR-3 窗口） | 2 |
| `/api/requirements/{id}/gate` | GET | 门禁域读面（轮次/issues/裁决态/三条件现算） | 3 |
| `/api/requirements/{id}/gate/open` | POST | 开闸（GR-2 三条件机械校验 → 建轮 + 排 gate_review） | 3 |
| `/api/requirements/{id}/gate/rounds/{rid}/ingest` | POST | issues 草稿确认入库（计数一致校验） | 3 |
| `/api/gate/issues/{iid}/verdict` | POST | 裁决三处置（落改/驳回/存疑） | 3 |
| `/api/gate/rounds/{rid}/release` | POST | 放行（GR-4 全绿 + 关卡互认） | 3 |
| `/api/requirements/{id}/gate/change-status` | GET | GR-3 变更检测 | 3 |
| `/api/requirements/{id}/gate/reopen` | POST | 人确认开新一轮 rN | 3 |

（其余批次2/3 细节端点在设计 lane / 门禁域实现时按需补入本表。）

## 5. 批次与验收

| 批次 | 内容 | 验证 |
|---|---|---|
| 0 | 在途契约实体化回归 + 基线 commit | verify-all 全绿（已完成） |
| 1 | 迭代域（表/API/1:1 守卫/迁移） | `scripts/verify-iter.mjs` + 全量回归 |
| 2 | 设计 lane | `scripts/verify-design.mjs` + 全量回归 |
| 3 | 门禁域 | `scripts/verify-gate.mjs` + 全量回归 |
| 4 | 前端（迭代列表/工作台/design/gate tab） | vite build + chrome-devtools 冒烟主链 |
| 5 | 收尾：verify-all 挂新脚本、文档指针、MEMORY | 全量回归 + 截图 |

## 6. 遗留（明确不做进本版）

- 误报模式库导出回 skills 仓文件（平台先落库沉淀）。
- issues 文件回写本地工作区（平台为裁决/放行权威，本地文件同步后议）。
- 真实 GLM agent 的 design/gate 生成质量联调（依赖余额；本轮以模拟任务口径验收）。
- 迭代归档态显式字段（当前由需求/门禁状态推导，demo 同口径）。
