# SDLC 产物协作平台 — 后端（Python）

需求质检 + 用例登记 + 设计/门禁的团队共享面后端。**FastAPI + Pydantic + PyMySQL**（uv 管理，
数据层 MySQL 8 —— EXEC-PLAN-K8s 阶段 2 起 SQLite 已退役，适配层见 `src/platform_sdlc/db.py`），
任务队列为进程内单执行通道（openai-agents；外置 runner 已于 2026-10-28 下线，飞书能力由进程内
Python 接入承载），openai-agents 亦驱动查询聊天助理。
前端见姊妹仓 `sdlc-platform-web`（构建产物拷入 `static/` 同源托管）。

设计依据与迁移映射：[docs/DESIGN-v3-Python迁移.md](docs/DESIGN-v3-Python迁移.md)（含守卫对照清单）。

## 快速开始

```bash
# ① 本地 MySQL（首次：拉镜像 + 建库；之后每次开发前起）
docker compose -f compose/mysql.dev.yml up -d
# ② .env 写数据源（首次：cp .env.example .env 后按需改）
#    SDLC_DB_DSN=mysql://root:sdlc@127.0.0.1:3306/sdlc
# ③ 启动
scripts/dev.sh        # uv sync + 启动 http://127.0.0.1:8888（前台，Ctrl-C 停止）
scripts/dev-log.sh    # 同上，日志落盘 logs/server.log（tail -f logs/server.log 跟随）
```

> 旧 SQLite 库迁移：`SDLC_DB_DSN=... uv run python scripts/migrate-sqlite-to-mysql.py --recreate`
> （dry-run 三硬门 + 18 表对账；本机开发库已迁完，data/platform.db 留档）。

IDE 断点调试：Debug `debug_server.py`（单进程无 reload，断点即生效）。

依赖：Python 3.12+（uv 可自装）、[uv](https://docs.astral.sh/uv/)；Node 18+（verify 脚本需要）。
（claude CLI / lark-cli 曾为外置 runner 与飞书抓取所依赖，随 runner 下线与飞书接入 Python 化已不再需要。）

## 任务执行通道（进程内 agent，任意 OpenAI 兼容端点）

```bash
export SDLC_AGENT_ENABLED=true
export SDLC_AGENT_BASE_URL=https://api.deepseek.com/v1   # 任意 OpenAI 兼容端点
export SDLC_AGENT_MODEL=deepseek-chat
export SDLC_AGENT_API_KEY=<key>
```

A/B 级 gate_review 评审的全新上下文扇出由进程内 `spawn_reviewer` 工具承载（扇出计数落工作区
`gate-fanout.json`，put_draft 按 it 核对留痕）；`POST /api/tasks/next` 保留为外部脚本的原子领取面
（verify-*.mjs 回归走它）。`scripts/verify-all.sh` 回归期间自动暂停进程内 agent，结束自动恢复。

## 环境变量（前缀 `SDLC_`，支持仓根 `.env`）

| 变量 | 默认 | 说明 |
|---|---|---|
| `SDLC_AGENT_ENABLED` | `false` | 打开进程内 agent 执行通道（启动时挂载轮询循环） |
| `SDLC_AGENT_BASE_URL` / `_MODEL` / `_API_KEY` | 空 | OpenAI 兼容端点三元组（执行 agent 与聊天助理共用） |
| `SDLC_AGENT_SKILLS_REPO` | `~/IdeaProjects/claude-sdlc-skills` | 任务 agent 引用的技能仓 |
| `SDLC_AGENT_WORKSPACE_ROOT` | `.` | 任务工作区根（`<root>/sdlc/<reqId>/`） |
| `SDLC_AGENT_TIMEOUT_MIN` / `_MAX_ITERS` / `_POLL_INTERVAL_MS` | `30` / `160` / `3000` | agent 领任务节奏与单任务上限 |
| `SDLC_GATE_TIMEOUT_MIN` | `60` | A/B 级 gate_review 扇出专项墙钟（主会话 + 4 子 run + 汇合共享同一 deadline） |
| `SDLC_FEISHU_APP_ID` / `SDLC_FEISHU_APP_SECRET` | 空 | 飞书应用凭据（docs_ai 服务端渲染链路，详见 `docs/DESIGN-飞书接入Python化.md`）。留空 = 无飞书能力：`feishu_fetch` 工具不注册、source_import 飞书源明确失败、`run_command` 白名单无 lark-cli（三处联动防模型 fallback） |
| `SDLC_FEISHU_MEDIA_MAX_BYTES` | `10485760` | 飞书大附件阈值：超限附件登记元数据标 `skipped:size-exceeded` 不下载（任务不阻断） |
| `SDLC_WRITE_TOKEN` | 空 | 设置后非 GET 的 `/api` 写接口要求 `X-Token` 头 |
| `SDLC_DB_DSN` | 必填（空即启动报错） | MySQL 数据源 `mysql://user:pass@host:3306/sdlc`（本地用 `compose/mysql.dev.yml`；K8s 走 Secret。密码特殊字符需 URL 百分号编码） |
| `SDLC_MEDIA_DIR` | `./data/media` | 媒体文件目录（K8s/PVC 指绝对路径） |

> **两仓版本对应**：进程内 agent 的飞书原文导入依赖姊妹仓 `claude-sdlc-skills` 的
> `parse_doc.py --media-manifest`（≥ 2026-10-28 版本；旧版未知参数 exit 3 会被误报为
> 用法错误）。`feishu_fetch` 工具启动时探测该参数，缺失即报「skills 仓版本不匹配」。

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
                              # 注意：库为 WAL 模式，直接 cp 库文件会丢未 checkpoint 数据——备份只信本脚本（sqlite3 .backup）
```

静态检查（贡献者）：`uv run pyright && uv run ruff check src tests`（pyright strict 全量）。

## 安全边界（继承 v1 共识）

无登录（内网互信）；默认绑 0.0.0.0（对外暴露前明确评估）；写防护可启用 `SDLC_WRITE_TOKEN`（写接口要求 X-Token 头）；
SQL 全参数化；数据可随时迁出（SQLite 单文件 + media 目录）。
