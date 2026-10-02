# SDLC 产物协作平台 — 后端（Python）

需求质检 + 用例登记 + 设计/门禁的团队共享面后端。**FastAPI + Pydantic + sqlite3**（uv 管理），
openai-agents 驱动任务执行侧与查询聊天助理。前端见姊妹仓 `sdlc-platform-web`（构建产物拷入 `static/` 同源托管）。

设计依据与迁移映射：[docs/DESIGN-v3-Python迁移.md](docs/DESIGN-v3-Python迁移.md)（含守卫对照清单）。

## 快速开始

```bash
scripts/dev.sh        # uv sync + 启动 http://127.0.0.1:8080（前台，Ctrl-C 停止）
```

依赖：Python 3.12+（uv 可自装）、[uv](https://docs.astral.sh/uv/)；Node 18+（仅 verify 脚本需要）；
claude CLI（外置 Runner 通道用）、lark-cli（飞书来源质检用）。

agent 执行侧 / 聊天助理（可选）：

```bash
export SDLC_AGENT_ENABLED=true
export SDLC_AGENT_BASE_URL=https://api.deepseek.com/v1   # 任意 OpenAI 兼容端点
export SDLC_AGENT_MODEL=deepseek-chat
export SDLC_AGENT_API_KEY=<key>
```

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
cd .. && 不需要；直接在本仓：
uv run pytest                # 单元回归（迁移器/仓储/守卫/API 键形）
scripts/verify-all.sh        # 黑盒全量回归（需服务已启动；10 个 verify-*.mjs）
node scripts/verify-agent.mjs      # agent 执行侧端到端冒烟（需模型余额）
node scripts/verify-assistant.mjs  # 查询聊天助理冒烟（需模型余额）
scripts/backup.sh            # SQLite 快照到 backups/（挂 cron，保留 30 份）
```

## 安全边界（继承 v1 共识）

无登录（内网互信）；默认绑 0.0.0.0（对外暴露前明确评估）；写防护可启用 `SDLC_WRITE_TOKEN`（写接口要求 X-Token 头）；
SQL 全参数化；数据可随时迁出（SQLite 单文件 + media 目录）。
