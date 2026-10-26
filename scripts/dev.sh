#!/bin/bash
# 启动后端：同步依赖 → 运行（0.0.0.0:8888）。前端构建产物放 server 的 static/（可选，见 sdlc-platform-web 仓）
# 用法：scripts/dev.sh          # 前台运行，Ctrl-C 停止
# agent 执行侧（可选）：SDLC_AGENT_ENABLED=true + SDLC_AGENT_BASE_URL/MODEL/API_KEY 环境变量
set -e
cd "$(dirname "$0")/.."

echo "[1/2] 同步 Python 依赖（uv sync）..."
uv sync --quiet

echo "[2/2] 启动 http://127.0.0.1:8888 ..."
mkdir -p data
# 以本目录为工作目录：./data（SQLite 库与媒体）相对此处解析
exec uv run uvicorn platform_sdlc.main:create_app --factory --host 0.0.0.0 --port 8888
