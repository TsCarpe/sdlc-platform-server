# SDLC 产物协作平台 — 后端（Python）

需求质检 + 用例登记 + 设计/门禁的团队共享面后端。**FastAPI + Pydantic + sqlite3**（uv 管理），
任务队列双执行通道（外置 runner spawn claude CLI / 进程内 openai-agents），openai-agents 亦驱动查询聊天助理。
前端见姊妹仓 `sdlc-platform-web`（构建产物拷入 `static/` 同源托管）。

设计依据与迁移映射：[docs/DESIGN-v3-Python迁移.md](docs/DESIGN-v3-Python迁移.md)（含守卫对照清单）。

## 快速开始

```bash
scripts/dev.sh        # uv sync + 启动 http://127.0.0.1:8080（前台，Ctrl-C 停止）
scripts/dev-log.sh    # 同上，日志落盘 logs/server.log（tail -f logs/server.log 跟随）
```

IDE 断点调试：Debug `debug_server.py`（单进程无 reload，断点即生效）。

依赖：Python 3.12+（uv 可自装）、[uv](https://docs.astral.sh/uv/)；Node 18+（verify 脚本与 Runner 需要）；
claude CLI（外置 Runner 通道用）、lark-cli（飞书来源质检用）。

## 任务执行通道（可选，二选一或并存）

外置 Runner（claude CLI 通道）：

```bash
scripts/runner.sh     # node runner/runner.mjs 轮询领任务、spawn claude CLI；SDLC_PLATFORM_URL 可指向其他实例
```

进程内 agent（openai-agents 通道，任意 OpenAI 兼容端点）：

```bash
export SDLC_AGENT_ENABLED=true
export SDLC_AGENT_BASE_URL=https://api.deepseek.com/v1   # 任意 OpenAI 兼容端点
export SDLC_AGENT_MODEL=deepseek-chat
export SDLC_AGENT_API_KEY=<key>
```

两通道抢同一任务队列（原子 claim 互斥，不会重复执行）；`scripts/verify-all.sh` 回归期间自动暂停两者，结束自动恢复。

## 环境变量（前缀 `SDLC_`，支持仓根 `.env`）

| 变量 | 默认 | 说明 |
|---|---|---|
| `SDLC_AGENT_ENABLED` | `false` | 打开进程内 agent 执行通道（启动时挂载轮询循环） |
| `SDLC_AGENT_BASE_URL` / `_MODEL` / `_API_KEY` | 空 | OpenAI 兼容端点三元组（执行 agent 与聊天助理共用） |
| `SDLC_AGENT_SKILLS_REPO` | `~/IdeaProjects/claude-sdlc-skills` | 任务 agent 引用的技能仓 |
| `SDLC_AGENT_WORKSPACE_ROOT` | `.` | 任务工作区根（`<root>/sdlc/<reqId>/`） |
| `SDLC_AGENT_TIMEOUT_MIN` / `_MAX_ITERS` / `_POLL_INTERVAL_MS` | `30` / `160` / `3000` | agent 领任务节奏与单任务上限 |
| `SDLC_WRITE_TOKEN` | 空 | 设置后非 GET 的 `/api` 写接口要求 `X-Token` 头 |
| `SDLC_DB_PATH` / `SDLC_MEDIA_DIR` | `./data/…` | 数据库与媒体目录 |
| `SDLC_PLATFORM_URL` | `http://127.0.0.1:8080` | Runner 指向的平台地址（仅 runner 侧读取） |

## 前端对接

后端同源托管 `static/`（不存在时纯 API 模式，前端可走 5173 代理开发）：

```bash
cd ../sdlc-platform-web && npm run build
rsync -a --delete dist/ ../sdlc-platform-server/static/
```

## 数据

`data/{platform.db, media/}`（首启自动建库 + 幂等迁移）。接入既有数据：
`cp -R <旧仓>/server/data/ ./`（迁移前先 `scripts/backup.sh` 快照）。

## 验收 / 回归

```bash
uv run pytest                # 单元回归（迁移器/仓储/守卫/API 键形）
scripts/verify-all.sh        # 黑盒全量回归（需服务已启动；10 个 verify-*.mjs）
node scripts/verify-agent.mjs      # agent 执行侧端到端冒烟（需模型余额）
node scripts/verify-assistant.mjs  # 查询聊天助理冒烟（需模型余额）
scripts/backup.sh            # SQLite 快照到 backups/（挂 cron，保留 30 份）
```

静态检查（贡献者）：`uv run pyright && uv run ruff check src tests`（pyright strict 全量）。

## 安全边界（继承 v1 共识）

无登录（内网互信）；默认绑 0.0.0.0（对外暴露前明确评估）；写防护可启用 `SDLC_WRITE_TOKEN`（写接口要求 X-Token 头）；
SQL 全参数化；数据可随时迁出（SQLite 单文件 + media 目录）。
