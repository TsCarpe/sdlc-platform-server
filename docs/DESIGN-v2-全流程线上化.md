# DESIGN v2 —— 全流程线上化

> 依据：2026-09-20 需求方拍板「平台都操作掉」：此前 9 步流程仅 PM 网页答复 1 步在线，质检生成、推送、
> 拉回答复、标准文档合成、发布、用例登记全部依赖本地终端跑 skill。
> 本文件记录 v2.0 的架构决策与数据契约；v1.x 的「平台只是共享交互面」定位由本版修订为**平台是唯一工作权威**。

## 1. 决策

| # | 决策 | 理由 |
|---|---|---|
| D21 | AI 架构 = **平台任务队列 + Runner**。平台只存任务状态；常驻 Runner（`runner/runner.mjs`，Node ≥18 零依赖）轮询领取，spawn headless claude（`-p --permission-mode bypassPermissions --output-format json`）执行 skill 管线，产物以草稿回传 | skill 的 prompt 资产（digest 模板/六层维度/合成规范/parse_doc 管线）零迁移零双源；符合 v1.2 D14「结构化由 agent 侧完成、平台不写解析器」的自然延伸；Java 8 后端不做 AI |
| D22 | **AI 产物一律两态**：先落 `draft_artifact`（人审关口），采纳（approve）才转正。预览页承担原 skill 流程的「暂停确认」 | 防 AI 产物直写共享权威；把「用户过目」红线搬到线上 |
| D23 | **线上化不改守卫，只改触发方**：approve 直接调用 `RequirementService.pushArtifacts` / `CaseService.syncUpsert`，发布按钮走现有 `POST /publish` | 发布锁定 / seq 答复保留 / P 编号漂移防护 / 乐观锁自动继承；verify-m2/m3/m4/guard 回归照跑即证零回归 |
| D24 | Runner 侧白名单：只允许 `PUT drafts` 与 `POST media`；任务 prompt 明令禁止 `/artifacts`、`/publish`、`/api/cases` | 双保险：即使 prompt 越权，守卫仍在服务端 |
| D25 | Runner 完成后**机械自查**才记 done：GET 草稿端点确认存在且结构合法（digest_bundle 须有 digest+audit+非空 items；standard 须 ≥200 字；cases 须非空 items） | 防 agent 未按指令回传就报成功 |
| D26 | 媒体先行：Runner 在生成阶段把 doc.media 逐个上传（D16 内容寻址幂等，无副作用），doc 随草稿回传，采纳时才推正式 doc | 草稿轻量化；missingMedia 语义不变 |
| D27 | 平台成唯一工作权威，本地 `sdlc/` 降级为 Runner 工作缓存；`sdlc-sync` skill 降级为平台不可用时的兜底通道 | 单一权威简化运维心智；本地通道保留（降级 = v1 全流程） |

## 2. 数据模型增量（schema.sql，全 IF NOT EXISTS）

```sql
task(id PK AUTOINCREMENT, req_id, type,        -- quality_check | standard_doc | case_gen
     status DEFAULT 'queued',                  -- queued → running → done|failed
     payload DEFAULT '', result DEFAULT '', runner DEFAULT '',
     created_by, created_at, updated_at)

draft_artifact(req_id, type,                   -- digest_bundle | standard | cases
     md, content_json, task_id, updated_at,
     PRIMARY KEY(req_id, type))                -- approve 转正后删除
```

## 3. API 增量（全部 /api 前缀，纳入 TokenFilter 写保护）

| 端点 | 说明 |
|---|---|
| `POST /api/requirements/{id}/tasks` `{type,note?}` | 建任务。守卫：404 不存在；400 非法 type；409 已发布（终态锁只约束 quality_check/standard_doc——**case_gen 放行**，用例登记本就是发布后的正常动作，同 v1 push-cases）；standard_doc 另须 clarifying + 全部已答（400）；同需求同 type 已有 queued/running → 409 防重复 |
| `GET /api/tasks?req_id=&status=` | 前端轮询（TaskPanel 4s） |
| `GET /api/tasks/next?runner=` | Runner 原子领取（SELECT 首条 queued → 带 status 条件 UPDATE，affected=1 即领取）；队列空 204 |
| `POST /api/tasks/{id}/done|failed` `{result|error}` | Runner 回报；仅 running 可回报（409 防竞态） |
| `POST /api/tasks/{id}/retry` | failed → queued |
| `POST /api/tasks/{id}/cancel` | queued → failed（排队中取消） |
| `PUT /api/requirements/{id}/drafts/{type}` `{md,contentJson,taskId}` | Runner 回传草稿（幂等覆盖）；published 409；digest_bundle 必须带 contentJson |
| `GET /api/requirements/{id}/drafts[/{type}]` / `DELETE` | 预览 / 丢弃 |
| `POST /api/requirements/{id}/drafts/{type}/approve` | digest_bundle → pushArtifacts；cases → syncUpsert（冲突 409+conflicts 透传）；standard 无 approve（发布走现有 `POST /publish`，前端编辑器确认终态） |

发布链路：standard_doc 任务 done → standard 草稿 → 平台编辑器（textarea + linkifyRefs 渲染预览 + 保存草稿）→ [发布] 确认弹窗（终态警示）→ 现有 publish 端点 → published + 草稿清除。

## 4. Runner 协议

```
启动：recoverStale()——超时（默认 30min）仍 running 的任务置 failed（上次中断遗留）
循环：GET /api/tasks/next → 204 睡 3s；否则按 type 取 prompt 模板
执行：cwd = 平台仓根（产物落 sdlc/<需求名>/），spawn headless claude，stdin 关闭，超时 SIGKILL
判定：exit 0 且无 is_error 且 selfCheck(草稿存在+结构合法) → done（result = SDLC_TASK_RESULT 摘要行）
      否则 → failed（错误 = 自查结论 / CLI 退出码 / 输出尾部）
```

- 技能源指向 **claude-sdlc-skills 仓内权威路径**（已安装副本可能落后）
- prompt 按任务类型内嵌：技能文件清单、平台端点、产物落盘路径、红线（D24）、媒体先行（D26）、
  要求最后一行输出 `SDLC_TASK_RESULT {...}` 供 result 摘要
- text 源需求不走 doc 块管线（无 lark-cli 依赖），冒烟与降级都可用

## 5. 前端（零新依赖）

- `TaskPanel.vue`（ReqDetail 顶部）：状态点亮按钮（开始质检/重新质检、生成标准文档）、任务状态行
  （轮询 4s、running 计时、failed 重试、queued 取消）、三块草稿卡
- 草稿卡：digest_bundle = 统计 chips + digest 正文折叠预览 + [采纳（进入待澄清）]；
  standard = 编辑/渲染预览双模式 + 保存草稿 + [发布（终态锁定）]；cases = 表格预览 + 409 冲突明细 +
  [覆盖平台版本]（剥 expectedVersion 重写草稿后强推）
- `render.js`：render/linkifyRefs 抽共享（ReqDetail 与 TaskPanel 复用）

## 6. 验收

- `scripts/verify-m5.mjs`（25 项断言）：排空残队 → 建任务守卫 5 条 → 领取/回报状态机 → failed+retry →
  草稿回传/回读/缺字段 400 → approve#1 转正 → 未答完 standard_doc 400 → **approve#2 carried=1（守卫继承）** →
  **approve#3 漂移 changed=1** → cases 登记 → **乐观锁冲突 409 且批次未应用** → 全答后 standard_doc 可建 →
  发布终态 → published 后建任务/写草稿/作答全 409
- `scripts/verify-all.sh`：编译 + m2/guard/m3/m4/m5 一口全绿
- Runner 冒烟：text 源建卡 → quality_check 任务 → Runner 全环（领取 → headless 执行 skill → 草稿回传）
  → 平台预览采纳
- 终验：真实飞书链接端到端（建卡 → 质检 → 采纳 → 答复 → 合成 → 发布 → 用例 → 浏览器五 tab 目检）

## 7. 边界与不做

- exec 浏览器测试与缺陷复验仍留本地 skill（执行结果不上链红线不变）；case_gen 任务只覆盖 cases 生成
- Runner 单并发（逐个领取执行）；多并发需要平台侧行信号量，暂无必要
- 飞书 webhook 通知、事件驱动自动质检维持 DEV-PLAN §7 v2 钩子不动
