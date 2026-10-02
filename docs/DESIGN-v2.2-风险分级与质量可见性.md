# DESIGN v2.2 —— 风险分级与质量可见性

> 依据：2026-09-26 业界对标调研（需求质检产出物 vs Fagan/INCOSE/QVscribe/RTM 实践）结论——平台产物链形态已够，短板在**价值自证与反馈回路**。本期四个增强：消化 sdlc-skill v0.10.0 的 risk_tier 契约（v1.3）、RTM 覆盖率、澄清处置通道、星级质量分。
> 范围决策（用户拍板）：血缘/跨轮 diff 本期不做（D3）；质量分仅详情页（列表逐行解析 audit JSON 是 N+1 反模式）；建卡表单不加 tier 下拉（等 push 后到补）。

## 1. 关键决策

| # | 决策 | 理由 |
|---|---|---|
| D19 | risk_tier 落库 requirement 表单列，建卡可选 + push-artifacts 的 `digest.risk_tier` **后到覆盖**（空=保留旧值，同 domains 语义） | 建卡常早于梳理定级；覆盖点在 pushArtifacts 内天然继承 published 409 锁（requirement 表无其他写路径、agent 工具面无写卡工具——已全路径核对） |
| D20 | 覆盖率 = detail 读时聚合 `coverage` 字段（forward/backward + danglingCases），不落库 | 沿 v1.1「反向索引读时聚合」原则；数据源全部已有（frPoints/coveredBy/caseRepo） |
| D21 | 澄清项处置四态 `pending/answered/rejected/deferred`；**已处置 = status IN (answered, rejected, deferred)**；驳回必填 note | REQ-2026-061 实践中 PM 用答复文本写「跳过」——诉求已被验证；业界 issue log 标配「拒绝」状态（防伪问题磨损 PM 信任） |
| D22 | status **显式写入 + SchemaMigrator 一次性回填**（`answer!='' AND status=''` → answered），SQL 不做永久降级 | SchemaMigrator 是 @Order(1) ApplicationRunner，补列回填同启动完成才对外服务，无迁移窗口；永久 `OR (status='' AND answer!='')` 是死分支 + 给未来消费方埋「必须记得 OR」的口径债 |
| D23 | A2 携带面扩为「seq + 问题文本不变」保留 answer **及 status/note**；携带过滤条件 = `answer 非空 OR status 非空` | 空答复直接驳回是最常见路径，原过滤（仅 answer 非空）会静默丢驳回记录——击穿「驳回重推不丢」承诺 |
| D24 | 质量分一律从 audit content 的 `items[].level` 现场数，不用顶部 red/yellow/blue 计数 | auditView 落库时 `Json.intVal(audit,"red",0)` 把缺失固化为 0，无法区分真 0 与缺失——用计数会把多阻断文档虚评五星 |
| D25 | 前端「已处置」口径收敛为共享模块 `frontend/src/disposition.js`，5 处消费点统一接入 | ReqDetail（computed/组头计数/未答过滤/进度条）与 TaskPanel（allAnswered 门控）各自独立实现口径，改后端必漂移（评审 P0-1：TaskPanel 漏改会禁用「生成标准文档」主流程） |
| D26 | 合成通道提示词（本仓 AgentScopeAgentExecutor + runner.mjs）声明 rejected/deferred 语义：rejected 不进定稿（连同 answer 忽略）、deferred 记遗留假设 | 提示词在本仓不在技能仓；驳回保留的半截 answer 不清洗（人审数据完整性），由合成层消化防泄漏 |

## 2. 数据契约（payload v1.3，全部可选）

```jsonc
// POST /api/requirements —— 建卡
{ "name": "…", "iter": "…", "sourceType": "feishu", "sourceUrl": "…", "submittedBy": "…",
  "risk_tier": "A" }              // v1.3 可选："A"|"B"|"C"，非法值 400；缺省空串

// POST /api/requirements/{id}/artifacts —— digest 增字段
{ "digest": { "summary": "…", "risk_tier": "A", … } }   // 非空且合法 → 覆盖建卡值（后到为准）；空 → 保留

// GET /api/requirements/{id} —— 详情返回（card 实体自动序列化）
{ "card": { "riskTier": "A", … },                    // 空串 = 未分级（前端不渲染徽章）
  "coverage": {                                        // 读时聚合；无 FR 或无用例时 pct=null
    "forward":  { "covered": 12, "total": 96, "pct": 12.5, "uncovered": [ { "id": "需求.FR-07", "name": "…", "group": "修改比赛" } ] },
    "backward": { "linked": 40, "total": 42, "pct": 95.2 },
    "danglingCases": [ "TC-03" ] },                    // points 空 or 含悬空 FR 引用（与 linked 严格互补的成因明细）
  "quality": { "stars": 1, "basis": "阻断 12 项 → ★" } }  // 无 audit 产物时整个字段不出现

// PUT /api/requirements/{id}/answers/{seq} —— body 增可选字段
{ "answer": "…",                                     // 原语义不变（作答）
  "status": "rejected|deferred",                      // 缺省 = 作答语义（status → answered）
  "note": "驳回/挂起理由" }                            // status=rejected 时必填非空（400）
```

## 3. 星级质量分口径（D1=A）

| 星 | 条件（按序取第一个命中） |
|---|---|
| ★★★★★ | 无阻断且无严重 |
| ★★★★ | 无阻断且 严重 ≤ 5 |
| ★★★ | 阻断 ≤ 2 |
| ★★ | 阻断 ≤ 5 |
| ★ | 其余 |

- 阻断 = items[].level 为 red 的条数，严重 = yellow；blue（建议）不计入（改进项非质量缺陷）
- items.level 无白名单（pushArtifacts 只校验澄清 items），现场数容忍异常值归非阻断档
- 口径随分数展示（前端 title），避免黑盒分数

## 4. 端点影响清单

| 端点 | 变更 |
|---|---|
| `POST /api/requirements` | 增可选 `risk_tier`（白名单校验） |
| `POST /api/requirements/{id}/artifacts` | digest.risk_tier 后到覆盖；返回不变 |
| `PUT /api/requirements/{id}/answers/{seq}` | 增可选 `status`/`note`；处置更新 status/note/answered_at（语义扩为「处置时间」） |
| `GET /api/requirements/{id}` | card.riskTier（实体自动）/ coverage / quality / items[].status+note |
| `GET /api/requirements/{id}/answers` | 输出增 status/note |
| `GET /api/requirements` | 行增 riskTier、answeredQ 口径改 status |

## 5. 守卫口径同步面（7 处，全部「已处置 = status IN ('answered','rejected','deferred')」）

1. `ClarifyItemRepoImpl.counts` — SQL 改按 status
2. `RequirementRepoImpl.listAll` answeredQ 子查询 — 同口径
3. `TaskService.createGuarded`（standard_doc 门槛）
4. `OrchestrationService.onAnswered`（调 counts，自动一致）
5. `ClarifyItemRepoImpl.answerMapBySeq` 过滤条件（D23）+ `RequirementService.pushArtifacts` 携带分支
6. `pushArtifacts` 重建行 status 策略：携带旧处置=旧值（含 note）/ skill 自带 answer=answered / 全新=pending；`insert` 扩列
7. `RequirementService.setAnswer`：rejected/deferred 下允许作答（重开转 answered）

## 6. 验收

- 批次 0（risk_tier）：verify-m2（建卡带 tier / push 覆盖 / 空串兼容）+ verify-guard（动了 pushArtifacts 本体）
- 批次 1a（coverage）：verify-m2 断言 forward/backward + REQ-2026-014/064 手工核对
- 批次 1b（处置通道）：verify-guard 新增**空 answer 驳回重推保留**用例（D23 的直接验证）+ verify-m5/m6/e2e/sync-real 回归
- 批次 2（质量分）：verify-m2 夹具断言（6R/13Y/12B → 按表核算）+ REQ-2026-064（12R/25Y/5B → ★）目检
- 最终 `scripts/verify-all.sh` 全绿
