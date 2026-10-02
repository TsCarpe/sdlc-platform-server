# DESIGN v2.1 — AgentScope 架构升级（JDK 21 / Boot 3 / DDD 多模块 / 进程内 Agent）

> 状态：Phase ① 已交付（本文记录已落地部分）；Phase ② / ③ 见文末规划。
> 前置决策（2026-09-20 与需求方确认）：① JDK 21 + Spring Boot 3.x；② 模型接入 GLM（OpenAI 兼容规范）；③ 架构对齐 edu-region（DDD 六模块）；④ 三步走分期交付。

## 0. 背景与目标

v2.0 已实现「平台任务队列 + 外置 Runner（runner.mjs spawn headless claude CLI）+ 草稿两态 + 人审采纳」。v2.1 在此基础上：

1. 运行时升级：Java 8 / Boot 2.7.18 → **JDK 21 / Boot 3.5.16**（jakarta 命名空间），解锁 AgentScope Java（要求 JDK 17+）
2. 工程结构对齐公司生态（edu-region）：单模块 → **DDD 六模块**
3. （Phase ②）执行引擎从「spawn claude CLI 蹭订阅」换成 **AgentScope HarnessAgent 进程内执行**，模型走 GLM 的 OpenAI 兼容端点
4. （Phase ③）**L2 生命周期编排**：澄清答完自动排 standard_doc、发布后自动排 case_gen，关卡仍由人按

**不变式（贯穿所有 Phase）**：agent 只能写草稿端点；采纳/发布永远是人点按钮；全量替换 / 保留人工作答 / 发布锁定 / 乐观锁守卫全部继承。

## 1. Phase ① 已交付：运行时升级 + DDD 六模块（零行为变更）

### 1.1 模块划分（依赖单向：start-app → controller → application → infrastructure → domain → common）

| 模块 | 职责 | 关键内容 |
|---|---|---|
| sdlc-platform-common | 通用支撑 | M / Json / ApiException / Times；common.web.TokenFilter（X-Token 写保护）、WebStaticConfig（SPA 回退 + 缓存策略） |
| sdlc-platform-domain | 领域层 | domain.model（9 实体）；domain.service（Requirement / Case / Task / Media——守卫逻辑所在）；domain.repository（9 仓储接口） |
| sdlc-platform-infrastructure | 基础设施 | infrastructure.repository（9 个 XxxRepoImpl，JdbcTemplate）；SchemaMigrator（启动补列） |
| sdlc-platform-application | 应用编排 | 占位；Phase ② SdlcAgentService / Phase ③ OrchestrationService 落位 |
| sdlc-platform-controller | 接口层 | 7 个 REST 控制器 + GlobalExceptionHandler |
| sdlc-platform-start-app | 启动 | SdlcPlatformApplication；application.yml / schema.sql / 前端 dist；产物 `sdlc-platform-start-app/target/sdlc-platform.jar` |

### 1.2 关键升级点与适配

- **jakarta 迁移**：全仓 javax 面仅 TokenFilter 4 个 servlet import → jakarta。
- **WebStaticConfig**：Spring 6 的 ResourceHttpRequestHandler 不再接受文件形式的 resource location，`classpath:/static/index.html` 改为目录形式 `classpath:/static/`。
- **构建 JDK**：本机基础 JDK 为 8，`scripts/dev.sh` 与 `scripts/verify-all.sh` 自动选用 JDK 21（优先 sdkman 自装，回退 IntelliJ 内置 JBR 21）；CI/他机需 JAVA_HOME 指向 21。
- **vite outDir**：`frontend/vite.config.js` 输出改为 `../backend/sdlc-platform-start-app/src/main/resources/static`。
- **验收**：`bash scripts/verify-all.sh` 全绿（M2 / guard / M3 / M4 / M5），API 契约、schema、前端零变化。

### 1.3 事故与重建记录（诚实留档）

升级过程中一次搬移脚本失误删除过未提交源码，经 IDEA Local History 恢复后有三个文件恢复为空，按下述方式重建（结构以运行库/调用方用法为权威，非逐字节还原）：

- `Times.java`：自 git 历史（5e96fa4）原样恢复
- `TestCase.java`：git 历史 v1.0 底 + v1.1 增量字段（verifyRefs / defectRefs）
- `Task.java`：从未提交，按 TaskRepo MAP/insert 与 TaskService 用法重建（字段与默认值一致）
- `schema.sql`：自 live SQLite（platform.db）`.schema` 提取重建（IF NOT EXISTS 化），列注释有损

## 2. Phase ② 已交付：AgentScope 进程内 Agent（与 runner.mjs 双通道）

### 2.1 依赖与配置
- `io.agentscope:agentscope-harness:2.0.3` + `agentscope-extensions-model-openai:2.0.3`（普通库方式，application 模块直依赖）
- 模型：GLM OpenAI 兼容端点；配置在 `application.yml` 的 `sdlc.agent.*`（enabled / poll-interval-ms / timeout-min / base-url / model / api-key / skills-repo / workspace-root），key 支持 `GLM_API_KEY` 环境变量覆盖、文件内默认值直用

### 2.2 代码落位（application 模块）
| 类 | 职责 |
|---|---|
| SdlcAgentProperties | 配置 record（@ConfigurationProperties） |
| TaskAgentExecutor | 执行器隔离接口（可整体替换回 CLI 模式或其他框架） |
| AgentScopeAgentExecutor | HarnessAgent 构建 + prompt（自 runner.mjs promptFor 移植，curl 配方改工具）+ SDLC_TASK_RESULT 抽取 |
| AgentPlatformTools | 工具面：get_requirement_card / get_digest_bundle / list_clarify_items / put_draft / upload_media / read_file（含按文件名模糊检索兜底）/ run_command（白名单 lark-cli/python3/node，单命令禁管道重定向） |
| SdlcAgentService | 轮询循环：claim(agentscope-inproc) → execute → 机械 selfCheck（草稿真回传且结构合法才 done）→ finish；启动恢复超时任务；pause/resume 开关 |

- 运行时验证：AgentTraceMiddleware（HarnessAgent 自带）逐轮记录 PRE/POST_REASONING/ACTING，工具级 `[tool]` 日志记录实参与命中路径
- 管理端点：`GET /api/agent/status`、`POST /api/agent/pause|resume`（controller 模块）；verify-all 回归期间自动 pause → 恢复，避免抢领验证任务
- **红线升级落地**：工具面上不存在任何 /artifacts、/publish、/api/cases 对应工具——从「prompt 禁止」升级为「工具面缺失」；run_command 白名单亦不含 curl
- runner.mjs 保留为降级通道（外置 claude CLI 蹭订阅模式），与进程内 agent 以原子 claim 天然互斥

### 2.3 实测结论（2026-09-20）
- 全链路验证通过：任务领取 → HarnessAgent 构建（7 工具注册）→ GLM 多轮 reasoning/tool-call → selfCheck → done/failed 如实回报
- 发现并修复：① sessionId 复用会带入上一轮污染上下文（改为每次执行唯一）；② read_file 路径拼写容错（模糊检索）；③ 弱模型不调工具直接放弃 → 靠 selfCheck 拦截，不会误记 done
- **遗留阻塞（非代码问题）**：GLM 账号对 glm-5.3/glm-4.6/glm-4.5-air 报 1113 余额不足（glm-4-flash 可用但其 agent 能力不足以稳定跑完多文件管线）。充值后 `POST /api/tasks/33/retry` 一键重试冒烟任务，或 `node scripts/verify-agent.mjs` 全新冒烟——配置无需改动
- 验收：`node scripts/verify-agent.mjs`（需可用模型）；`bash scripts/verify-all.sh` 全绿（agent 暂停机制已并入回归）

## 3. Phase ③ 已交付：L2 生命周期编排（确定性事件钩子，非 agent 决策）

### 3.1 规则与铁律
- **触发点 ①**：`PUT /answers/{seq}` 成功后（controller 层挂钩）——澄清全部答完 && 从未建过 standard_doc 任务 → 自动排队 standard_doc（createdBy=`auto:clarify-complete`）
- **触发点 ②**：`POST /publish` 成功后——自动排队 case_gen（createdBy=`auto:published`）
- 铁律：
  - **守卫全继承**：OrchestrationService → TaskService.createAuto → createGuarded（状态机 / 全部答完 / countActive 防重），人工与自动走同一套守卫（人工建同类任务会被 409 防抖）
  - **只自动一次**：anyTaskOfType（不限状态，含人工建过或已取消）→ 人 cancel 即终局，改答不复活；后续轮次由人主导
  - **编排失败绝不连累主操作**：钩子内一切异常吞掉记日志（`[L2]` 前缀），作答/发布永远 200
- 代码落位：`application` 模块 `OrchestrationService`（钩子逻辑）+ `domain` 的 `TaskService.createGuarded/createAuto/anyTaskOfType`（守卫共用）；controller 在主操作成功后调用，避免 service 相互依赖成环
- 前端：TaskPanel 对 createdBy 以 `auto` 开头的任务显示「自动」chip；cancel（仅 queued）是人手中的逃生门

### 3.2 既有脚本适配
- verify-m5：新增「L2 全答完自动排 standard_doc」断言（验后取消，保持后续人工断言的干净世界）；自带 agent 暂停/恢复（仅当其自行暂停时才恢复，兼容 verify-all 的外层暂停）
- verify-e2e：手动建 standard_doc/case_gen 任务处吞 409（L2 已自动排队 = 复用）
- verify-all：脚本清单纳入 verify-m6

### 3.3 验收（2026-09-20 全绿）
`node scripts/verify-m6.mjs` 12 项断言全过：未答完不排 / 全答完自动排（createdBy 标记）/ 改答不重复建 / 只自动一次 / 人工建同类 409 / 取消即终局不复活 / 发布后自动排 case_gen；`bash scripts/verify-all.sh`（m2+guard+m3+m4+m5+m6）全部通过。