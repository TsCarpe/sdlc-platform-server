"""K8s preStop 钩子（EXEC-PLAN-K8s 阶段 1）：POST /api/agent/pause 让进程内 agent 停领新任务。

幂等且失败容忍（任何异常 exit 0，绝不阻断 pod 终止）：pause 只止损「新任务领取」，
SIGTERM 后 uvicorn 30s 收尾；在跑任务的回收由启动期 expire_stale + _stale_sweeper 300s
兜底（AgentLoop.stop 是 cancel 非软着陆——现状设计，阶段 1 不引入软着陆改造）。
stdlib urllib 实现：python:3.12-slim 无 curl 时的最小依赖面（runtime 阶段的 curl 是给
agent run_command 用的，钩子不自依赖它）。
"""

import sys
import urllib.request

try:
    req = urllib.request.Request("http://127.0.0.1:8888/api/agent/pause", data=b"", method="POST")
    with urllib.request.urlopen(req, timeout=3) as resp:
        print(f"prestop: agent pause -> HTTP {resp.status}")
except Exception as exc:  # noqa: BLE001 —— 钩子必须永远成功退出
    print(f"prestop: pause 未生效（容忍，在跑任务靠 expire_stale 兜底）：{exc}", file=sys.stderr)
sys.exit(0)
