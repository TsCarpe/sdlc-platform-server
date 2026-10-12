#!/bin/bash
# v3 全量回归：全部验证脚本（v1.0 守卫 → v1.1 追溯 → v1.2 原文块 → v2.0 任务/草稿 → v2.4 迭代/设计/门禁 → 令牌/agent）
# 用法：scripts/verify-all.sh [BASE]   （需平台已运行，默认 http://127.0.0.1:8080）
set -e
cd "$(dirname "$0")/.."

# nvm node
if [ -d "$HOME/.nvm/versions/node" ]; then
  NODE_BIN="$(ls -d "$HOME"/.nvm/versions/node/*/bin 2>/dev/null | sort -V | tail -1)"
  [ -n "$NODE_BIN" ] && export PATH="$NODE_BIN:$PATH"
fi

BASE="${1:-http://127.0.0.1:8080}"
echo "== 平台健康检查 =="
curl -sf "$BASE/api/health"
echo

# 回归期间暂停 Runner（否则它会领走 verify-m5 建的测试任务产生竞态），结束后恢复
RUNNER_WAS_RUNNING=0
if pgrep -f "runner/runner.mjs" >/dev/null 2>&1; then
  RUNNER_WAS_RUNNING=1
  pkill -f "runner/runner.mjs"
  echo "== 已暂停 Runner（回归后自动恢复）=="
fi
# v2.1：同样暂停进程内 agent（否则它会抢领 verify-m5 建的测试任务产生竞态）
AGENT_WAS_PAUSED=0
if curl -sf "$BASE/api/agent/status" 2>/dev/null | grep -q '"paused":false'; then
  curl -sf -X POST "$BASE/api/agent/pause" >/dev/null 2>&1 && AGENT_WAS_PAUSED=1
  echo "== 已暂停进程内 agent（回归后自动恢复）=="
fi

FAIL=0

# 恢复动作收敛为唯一出口（EXIT trap）：正常结尾与 Ctrl-C/异常中断 alike 都恢复被暂停的执行通道（AUDIT D8-3）
restore() {
  if [ "$RUNNER_WAS_RUNNING" = "1" ] && ! pgrep -f "runner/runner.mjs" >/dev/null 2>&1; then
    nohup scripts/runner.sh > /tmp/sdlc-runner.log 2>&1 &
    echo
    echo "== Runner 已恢复 =="
  fi
  if [ "$AGENT_WAS_PAUSED" = "1" ]; then
    curl -sf -X POST "$BASE/api/agent/resume" >/dev/null 2>&1 && echo "== 进程内 agent 已恢复 =="
  fi
}
trap restore EXIT

# 写令牌端到端冒烟（自建临时实例：独立库+随机端口+SDLC_WRITE_TOKEN，不依赖主服务配置）
echo
echo "== verify-token（自建实例）=="
node scripts/verify-token.mjs || FAIL=1

for s in verify-m2 verify-guard verify-m3 verify-m4 verify-m5 verify-m6 verify-delete verify-iter verify-design verify-gate; do
  echo
  echo "== $s =="
  node "scripts/$s.mjs" "$BASE" || FAIL=1
done

# 进程内 agent 端到端（可选）：需真实模型配置与余额——就绪才跑，未配置打印 SKIP 不红（AUDIT D7-1 补链）
if [ -n "$SDLC_AGENT_BASE_URL" ] && [ -n "$SDLC_AGENT_MODEL" ] && [ "${SDLC_VERIFY_AGENT:-}" = "1" ]; then
  echo
  echo "== verify-agent（SDLC_VERIFY_AGENT=1 且模型已配置）=="
  node scripts/verify-agent.mjs "$BASE" || FAIL=1
  echo "== verify-assistant =="
  node scripts/verify-assistant.mjs "$BASE" || FAIL=1
else
  echo
  echo "== verify-agent SKIP（需 SDLC_VERIFY_AGENT=1 且 SDLC_AGENT_BASE_URL/MODEL 已配置——agent 通道端到端未纳入本轮）=="
fi

echo
if [ "$FAIL" = "0" ]; then
  echo "全部回归通过 ✅"
else
  echo "存在失败项 ❌"
  exit 1
fi
