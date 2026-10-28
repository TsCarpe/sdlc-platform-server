#!/usr/bin/env python3
"""任务模板两处一致性守护（v4-3，DESIGN-v4 §6.1：模板双写从「注意」升为守护；
runner 2026-10-28 下线后收敛为单通道两处）。

每新增一种任务类型必须两处同步，漏一处 CI 红：
  ① src/platform_sdlc/agent/prompts.py sys_prompt 的 if task_type == "x" 分支集
    （goal_prompt 分支集须为子集——漏 goal 分支落 case_gen 兜底句，行为漂移不崩）
  ② task_rules.py DRAFT_TYPES ∪ NO_DRAFT_TYPES 键集 ↔ ① 模板分支集逐键相等
    （任务→草稿映射的单一数据源；loop.py 运行时消费，走错映射即落库错草稿类型）

仿姊妹仓 claude-sdlc-skills/scripts/check_adversarial_core.py 的「提取 + 比对 + exit 1」模式；
提取不出分支 = 红（宁红不静默——正则失效于结构变更时应显式失败而非假绿）。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PROMPTS = REPO / "src" / "platform_sdlc" / "agent" / "prompts.py"
TASK_RULES = REPO / "src" / "platform_sdlc" / "task_rules.py"

_PROMPT_BRANCH = re.compile(r'if task_type == "([a-z_]+)"')
_RULES_ENTRY = re.compile(r'"([a-z_]+)":\s*"([a-z_]+)"')  # task_rules.py：双引号键值
_NO_DRAFT_ENTRY = re.compile(r'NO_DRAFT_TYPES.*?frozenset\(\{(.*?)\}\)', re.S)


def extract_prompt_branches(text: str) -> tuple[set[str], set[str]]:
    """sys_prompt 分支集 + goal_prompt 分支集（goal 以 case_gen 兜底，期望 = sys - {case_gen}）。"""
    sys_m = re.search(r"def sys_prompt\(.*?\n(.*?)\n\ndef ", text, re.S)
    goal_m = re.search(r"def goal_prompt\(.*?\n(.*?)\n\ndef ", text, re.S)
    if sys_m is None or goal_m is None:
        raise RuntimeError("prompts.py 结构变更：sys_prompt/goal_prompt 函数体提取失败")
    return set(_PROMPT_BRANCH.findall(sys_m.group(1))), set(_PROMPT_BRANCH.findall(goal_m.group(1)))


def extract_rules(text: str) -> tuple[dict[str, str], set[str]]:
    dt_m = re.search(r"DRAFT_TYPES: dict\[str, str\] = \{(.*?)\}", text, re.S)
    nd_m = _NO_DRAFT_ENTRY.search(text)
    if dt_m is None or nd_m is None:
        raise RuntimeError("task_rules.py 结构变更：DRAFT_TYPES/NO_DRAFT_TYPES 提取失败")
    draft_types = dict(_RULES_ENTRY.findall(dt_m.group(1)))
    no_draft = set(re.findall(r'"([a-z_]+)"', nd_m.group(1)))
    return draft_types, no_draft


def check_consistency(prompt_sys: set[str], prompt_goal: set[str],
                      rules_dt: dict[str, str], rules_nd: set[str]) -> list[str]:
    """纯函数：返回违例清单（空 = 通过）。tests 直接单测本函数。"""
    problems: list[str] = []
    # goal_prompt 以 case_gen 为兜底 return（无 if 分支），其余类型须逐一致——新增类型漏 goal 分支
    # 会静默落 case_gen 兜底句（行为漂移不崩），故用正向相等式而非子集式
    goal_expected = prompt_sys - {"case_gen"}
    if prompt_goal != goal_expected:
        problems.append(f"goal_prompt 分支与 sys_prompt 不一致（期望 {sorted(goal_expected)}）：{sorted(prompt_goal)}")
    all_task_types = rules_dt.keys() | rules_nd
    if prompt_sys != all_task_types:
        problems.append(f"①② 任务类型面不一致：模板分支={sorted(prompt_sys)} vs 规则键={sorted(all_task_types)}")
    return problems


def main() -> int:
    try:
        prompt_sys, prompt_goal = extract_prompt_branches(PROMPTS.read_text(encoding="utf-8"))
        rules_dt, rules_nd = extract_rules(TASK_RULES.read_text(encoding="utf-8"))
    except RuntimeError as e:
        print(f"FAIL 提取失败（宁红不静默）：{e}")
        return 1
    problems = check_consistency(prompt_sys, prompt_goal, rules_dt, rules_nd)
    if problems:
        for p in problems:
            print(f"FAIL {p}")
        return 1
    print(f"任务模板两处一致性通过：{sorted(prompt_sys)}（草稿 {len(rules_dt)} 类 / 无草稿 {sorted(rules_nd)}）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
