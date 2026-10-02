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

echo "[runner] 平台=${SDLC_PLATFORM_URL:-http://127.0.0.1:8080}"
exec node runner/runner.mjs
