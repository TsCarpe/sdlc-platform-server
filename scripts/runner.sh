#!/bin/bash
# 启动 SDLC 平台任务 Runner（前台运行；后台用法：nohup scripts/runner.sh > /tmp/sdlc-runner.log 2>&1 &）
# 依赖：node ≥18（nvm）、claude CLI（headless）、lark-cli（飞书输入的质检任务需要）、python3
set -e
cd "$(dirname "$0")/.."

# nvm node（lark-cli / node 均在其 bin 下）
if [ -d "$HOME/.nvm/versions/node" ]; then
  NODE_BIN="$(ls -d "$HOME"/.nvm/versions/node/*/bin 2>/dev/null | sort -V | tail -1)"
  [ -n "$NODE_BIN" ] && export PATH="$NODE_BIN:$PATH"
fi

# 稳定 runner 名（P1-5）：默认按主机名派生，不随进程 pid 变化——recover-stale 按名字归属，
# pid 名重启即变会让上一进程的 running 永远无人认领；同主机多实例必须显式设置不同名字
export SDLC_RUNNER_NAME="${SDLC_RUNNER_NAME:-local-$(hostname)-runner}"
echo "[runner] 名称=${SDLC_RUNNER_NAME} 平台=${SDLC_PLATFORM_URL:-http://127.0.0.1:8888}"
exec node runner/runner.mjs
