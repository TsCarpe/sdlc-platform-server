#!/bin/bash
# 启动后端并落盘日志：logs/server.log（终端同时可见，Ctrl-C 停止）。
# 查看历史/跟随：tail -f logs/server.log
set -e
cd "$(dirname "$0")/.."

mkdir -p data logs
echo "[1/2] 同步 Python 依赖（uv sync）..."
uv sync --quiet

echo "[2/2] 启动 http://127.0.0.1:8888（日志 → logs/server.log）..."
# 管道 tee 代替 dev.sh 的 exec：日志既出终端又落盘；*.log 已被 .gitignore 忽略
uv run uvicorn platform_sdlc.main:create_app --factory --host 0.0.0.0 --port 8888 2>&1 | tee -a logs/server.log
