# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目定位

SDLC 产物协作平台后端（Python/FastAPI，v3.0 起自 Java 迁移）。单服务单库（SQLite 单连接单写者），
任务队列 + 双执行通道（外置 runner.mjs spawn claude CLI / 进程内 openai-agents），产物走「草稿 → 人工采纳/发布」两态。
前端在姊妹仓 `sdlc-platform-web`（构建产物拷入 `static/` 同源托管）。权威文档：人读契约 = `docs/ER图与业务流程.md` §13、
机器契约 = `docs/openapi.json`（CI 快照门）；`docs/DESIGN-v4-需求平台权威化.md`（v4 设计）与 `docs/EXEC-PLAN-v4.md`（实施计划）。
环境变量（SDLC_AGENT_* / SDLC_WRITE_TOKEN 等）见 README。

v4 起需求原文以块表为权威（`req_block` 一行一块锚点即主键，`req_comment` 双 origin 评论）：
source_import 任务建卡自动排、完成回调服务端直读 `tasks/<task_id>/out/doc.json` 落库（不经 LLM 上下文）；
质检纯分析读块模型（`GET …/doc.json` / `comments.json` curl 直落）；分源封存守卫管住全量重导。
新增任务类型必须三处同步（prompts.py ∪ runner.mjs promptFor ∪ task_rules/DEFAULT_RULES），
`scripts/check_task_templates.py` 为 CI 第五道门。

## 常用命令

```bash
scripts/dev.sh                 # uv sync + 启动 8888（前台；cwd 必须是仓根：./data 等相对路径依赖它）
scripts/dev-log.sh             # 同上 + 日志落盘 logs/server.log
scripts/runner.sh              # 外置 Runner（node runner/runner.mjs 轮询领任务，spawn claude CLI）
uv run pytest                  # 单元回归；单跑一条：uv run pytest tests/test_guards.py -k 关键词
uv run pyright && uv run ruff check src tests   # 类型 strict + lint
scripts/verify-all.sh          # 黑盒全量回归（需服务已启动；自动暂停 Runner 与进程内 agent 防抢任务，结束自动恢复）
node scripts/verify-agent.mjs  # agent 执行侧端到端冒烟（需模型余额）
scripts/backup.sh              # SQLite 快照
```

IDE 断点：直接 Debug `debug_server.py`（单进程无 reload，等价 dev.sh）。

## 架构总览（跨文件大图）

- 装配链：`main.create_app()` → `run_migrations` → `Repos.create` / `Services.create` → `AppContext`
  （`runtime.py`，路由层唯一共享对象）→ `register_routers` → `_mount_static`（SPA 同源托管，index no-cache；
  `static/` 不存在即纯 API 模式）。lifespan 在 `SDLC_AGENT_ENABLED` 时启动 `AgentLoop`。
- 任务双通道互斥：外置 runner 与进程内 `AgentLoop` 抢同一队列——runner 走 `POST /api/tasks/next`、进程内直调服务层，同归 `task_repo.claim()`
  （条件 UPDATE，affected=1 才算领到）；`finish()` 仅 running 可回报终态。回归用 `/api/agent/pause` + 停 runner
  防竞态（verify-all.sh 已内置）。
- 路由层惯例：async 路由里的同步服务调用一律 `asyncio.to_thread(...)`——SQLite/文件 IO 不得冻结事件循环
  （进程内 agent 轮询、助理 SSE 流与请求共享同一循环）。
- L2 自动编排：`orchestration.py` 的 `on_answered` / `on_published` 挂在作答/发布路由成功之后（编排失败不连累
  主流程）；「只自动一次」判断走 `task_repo.exists_by_type`（EXISTS 直查，不信 list 的 LIMIT 100 窗口）。
- agent 工具面（`agent/tools.py` ALL_TOOLS）：读需求卡/澄清项/草稿/用例只读；写入仅 `put_draft`（草稿）与
  `upload_media`——正式产物无 agent 写通道，唯一人道是平台「采纳/发布」。查询助理（`routers/assistant.py`）
  SSE 流式，独立 SQLite 会话库。
- 错误面：服务层抛 `ApiException(status, msg)`（`common.py`）→ 全局异常处理器转 `{error}`；409 冲突走
  `ConflictException` 带 conflicts；入参宽松绑定 helpers（`jtext` / `jint_or_400` 等）集中在 `common.py`。
- 数据：表结构集中在 `schema.sql`；需求删除为软删除（`deleted_at` 打标，running 任务先拒、queued 批量取消）。

## 硬约束

- Python 3.12+ / uv；类型全量 strict（pyright）+ ruff；pytest 为单元回归，verify-*.mjs 为黑盒验收主力。
- SQLite 单连接 + RLock：`db.tx()` = 服务操作粒度事务（可重入 = 嵌套加入外层）；循环体内无数据库查询；SQL 全参数化 + 显式排序。
- schema 变更：schema.sql 加 `IF NOT EXISTS` 建表 + `migrations.py` 判列补列（幂等，八步惯例）。
- 分层单向：main → routers → services → repositories → models；routers 不碰仓储。跨域流程汇聚点 =
  `orchestration.py`（作答/发布/开闸三钩子，挂路由成功后）+ `task.approve`（草稿采纳分流）；
  保留互调：`task.create_guarded → designs.assert_gen_allowed`（写面守卫）、`gate.checks → designs.view`（读面）。
- 出参 camelCase（键序 = 契约序，见 schemas.py）；入参宽松绑定（dict + 服务层校验，文案与 Java 版逐字）；错误统一 `{error}`，乐观锁 409 带 conflicts。
- 前端产物 `static/` 为部署物不入库；数据 `data/` 不入库。

## 业务红线（改代码前必读，验收 = scripts/verify-*.mjs）

推送全量替换；重推按 seq+问题文本保留已答内容（P 编号漂移不携带）；发布终态锁（拒推/拒草稿/拒任务/拒作答）；
用例乐观锁 409+conflicts；sdlc-test 来源用例平台只读+评论；L2 编排只自动一次（人取消即终局）；
任务原子 claim 双通道互斥；AI 生成内容不得直接成为正式产物（草稿→采纳两态不变）——唯一例外是
服务端直读脚本输出的确定性管道（source_import 完成回调）；导入分源封存（全量重导有平台数据后拒绝）；
GR-2/3/4 平台机械校验 + 放行关卡互认。
细节见 docs/DESIGN-v3-Python迁移.md §5 守卫对照清单（含 v4 增补行）与 ER 图 §14 红线索引。

## Git 惯例

commit message 中文，主题开头，如 `修复：…`、`P5：…`。`data/`、`static/`、`.venv/`、`backups/` 不入库。
