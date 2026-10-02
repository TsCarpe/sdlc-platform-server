# DESIGN-v3：后端 Java → Python 整体迁移（v3.0）

v3.0 起后端为 **Python 3.12+ / FastAPI / Pydantic / sqlite3**（uv 管理），一次性替换 Java（Spring Boot 3.5 / JDK 21，六模块 DDD）。API 契约键形级 + 文案级兼容：`scripts/verify-*.mjs` 全套（含 v1.0 守卫至 v2.4 门禁）为迁移验收口径，前端与 runner.mjs 零改动。

## 1. 目录与 Java 六模块映射

```
server/src/platform_sdlc/
├── main.py            # start-app：装配、异常处理器族、静态托管五行为、lifespan（agent loop 启停）
├── config.py          # application.yml：pydantic-settings（SDLC_ 前缀 env）+ DataDir 解析规则
├── db.py              # Hikari pool=1：sqlite3 单连接 + RLock；tx() = 服务操作粒度事务（REQUIRED 传播）
├── migrations.py      # SchemaMigrator 八步：补列/补表/回填×4/账本初值/uq_req_iter_alive
├── common.py          # common：ApiException/Times/Json 取值助手（文案逐字）
├── models.py          # domain.model：13 表实体 dataclass + from_row（null 归一口径同 Java MAP）
├── schemas.py         # 实体→camelCase 出参（键序=Java 声明序）；入参宽松绑定（dict + 服务层校验）
├── repositories/      # infrastructure：11 仓储，SQL 逐行移植（claim 条件 UPDATE 原子语义）
├── services/          # domain.service ×7 + orchestration.py（L2 钩子）
├── routers/           # controller：10 路由 49 端点
└── agent/             # application 的 agent 面（openai-agents）
    ├── context.py     # TaskRunContext：只读服务 + put_draft/upload_media（正式写通道不存在=红线）
    ├── tools.py       # 10 工具（5 只读 + 草稿/媒体 + 文件 + run_command 九命令白名单）
    ├── prompts.py     # sysPrompt/goal/速查卡/续跑提示（自 AgentScopeAgentExecutor 逐字移植）
    ├── executor.py    # 六类任务执行器：chat_completions + 兼容端点 + tracing off；max_turns=maxIters
    ├── loop.py        # SdlcAgentService：claim→execute→selfCheck→finish、expireStale、pause/resume
    └── assistant.py   # P5 新增：查询聊天助理（只读工具 + 自实现 Session + SSE）
```

## 2. 契约兼容要点（评审修订后落地）

- **序列化**：响应键名 camelCase（schemas.wire）；`RequirementCreateRequest.risk_tier` 是下划线键（@JsonProperty）；查询参数混用（`/api/tasks?req_id=` vs `/api/cases?reqId=`）为既有契约。
- **错误体**：ApiException/405/415/参数错误统一 `{error}`；乐观锁 409 携带 `conflicts:[{id,currentVersion,expectedVersion}]`；`/api/tasks/next` 空队列 204 无 body。
- **NON_NULL 子集**：RequirementDetail 等 8 视图 null 键不输出（服务层过滤）；Requirement/Task 等实体 null 键照常输出。
- **静态五行为**：/assets 365 天缓存、index.html no-cache、无扩展名路径 SPA 回退、favicon.ico→svg 重定向、未命中 404 `{error}`；API 面路径命中方法不符 → 405（对齐 Spring）。
- **媒体**：mediaId `^m-[0-9a-f]{12}$`、双引号 ETag 304 协商、immutable 缓存、X-Media-Mime 上传头。

## 3. 并发模型

单连接 + RLock（RLock 可重入 → 服务层 `tx()` 嵌套即 REQUIRED 传播）；写路径多步操作全程持锁（复刻「@Transactional 持连接」的 TOCTOU 防护：settleTerminal / delete / create / pushArtifacts / syncUpsert / gate 全部写守卫）。agent loop 为 asyncio 单循环串行；工具/服务层阻塞调用统一 `asyncio.to_thread`（P4 活性断言：任务执行中 /health 与 /api/agent/pause 可响应）。

## 4. agent 执行侧（openai-agents 替代 AgentScope-Java）

- 模型接线：`set_default_openai_client(AsyncOpenAI(base_url, api_key))` + `set_default_openai_api("chat_completions")` + `set_tracing_disabled(True)`；配置 env：`SDLC_AGENT_ENABLED/BASE_URL/MODEL/API_KEY/SKILLS_REPO/WORKSPACE_ROOT`（**不落配置文件；注意 application.yml 曾内置 DeepSeek key，已要求轮换**）。
- 工具面红线：context 不含正式写入服务（工具面缺失 > prompt 禁止）；run_command 九命令白名单 + 禁命令替换 + 输出截断照搬。
- prompt 逐字移植；DSML 泄漏检测（AgentScope 特征串）改写为「最终输出缺 SDLC_TASK_RESULT 行 → 换会话续跑一次」；不强制 output_type（DeepSeek chat_completions 结构化输出支持度未验证，正则解析保留为权威）。
- runner.mjs 通道零改动（HTTP 面），双通道原子 claim 互斥语义不变。
- P5 新增查询助理：只读工具面 + 自实现 Session（独立 assistant.db）+ SSE 流式（`POST /api/assistant/chat`）。

## 5. 守卫对照清单（迁移验收 = 逐条打钩）

| 红线 | 落点 | 验收 |
| :--- | :--- | :--- |
| 重推保留答复 / P 编号漂移 | services/requirement.push_artifacts | verify-m2 / test_guards |
| 发布终态锁（拒推/拒草稿/拒任务/拒作答） | 同上 + services/task | verify-guard / test_guards |
| 状态机 submitted→reviewing→clarifying→published | services/requirement | test_guards |
| 用例乐观锁 409+conflicts / 全量替换保留表单用例 | services/case | verify-guard / verify-m3 |
| 编辑权（sdlc-test 只读+评论） | services/case.form_update | verify-m3 |
| L2 只自动一次 / 发布收尾取消 queued | services/orchestration + task | verify-m6 |
| 任务 claim 原子互斥 / superseded 兜底 / expireStale | repositories/task + agent/loop | verify-m5 |
| GR-2/3/4 机械校验 + 关卡互认 | services/gate | verify-gate |
| 软删根过滤 / REQ 编号不复用 / 迭代名释放 | repositories/requirement | verify-delete |
| 设计两值落库 / 门禁两值落库 / 六态推导 | services/design + gate | verify-design / verify-gate |
| seq_case_no 单调账本（硬删不复用） | migrations + repositories/test_case | test_repos |

## 6. 数据与部署

- 数据：`server/data/{platform.db, media/, assistant.db}`（自 backend/data 迁移，备份快照见 backups/）；schema.sql 原样（打包进 `src/platform_sdlc/schema.sql`）。
- 启动：`scripts/dev.sh`（= npm build + uv sync + `uv run uvicorn platform_sdlc.main:create_app --factory --host 0.0.0.0 --port 8080`，cwd=server/）；常驻用 launchd/systemd，**WorkingDirectory 必须是 server/**（相对路径解析依赖它）。
- 前端：vite outDir → `server/static`（gitignore）；`/api` 代理 8080 不变。
- 验收：`scripts/verify-all.sh`（10 脚本）+ `node scripts/verify-agent.mjs`（需模型）+ `node scripts/verify-assistant.mjs`（需模型）+ pytest（`cd server && uv run pytest`）。
