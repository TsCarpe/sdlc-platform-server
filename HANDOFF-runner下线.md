# HANDOFF：runner 下线执行交接（2026-10-28）

> 给实施会话的自足交接。前置改造「飞书接入 Python 化」**已实施完毕并真实验证通过**
> （EXEC-PLAN §0 的 runner 下线前置条件已满足）；本文件只讲 runner 下线本身。
> 用户已拍板开始下线（2026-10-28）。

## 一、必读（按序）

1. `docs/EXEC-PLAN-K8s部署与中间件改造.md` §0 / §8-4 —— runner 下线的决策依据
2. `docs/DESIGN-飞书接入Python化.md`（含 §13 修订）—— 前置改造全貌与验收状态
3. `CLAUDE.md` —— 项目铁律（改 CLAUDE.md 自身是本任务的一部分）

## 二、前置改造已完成（新会话不需重做）

- 进程内 agent 飞书能力全量落地：`src/platform_sdlc/integrations/feishu/` 七模块
  （client/wiki/comments/docx/oracle/media/fetch），依赖注入五处接线
  （config/context/tools 工厂/executor/prompts 三处能力联动）。
- **LLM 端到端真实验证通过**（经两次返工，勿信更早的「通过」表述）：
  主样本任务 done——正文 511 块与黄金 type 逐项全等 + textSha 一致；评论 21 条
  （拍板规则预期多收，黄金 lark-cli 规则 11 条）；媒体 48 ok + 30MB m4a
  skipped:size-exceeded 豁免；feishu-forbidden 重试 409；verify-all 全绿。
- **防假绿判据**（若需复验）：产物目录须含 `media-manifest.json`+`raw/`；
  `doc.json` 的 `source.documentId/revisionId` 非空；无 lark-cli 三件套
  （doc.md/fetch.json）。历史假绿两案已修：feishu_fetch 路径合法面（7afa527）、
  平台终检豁免 skipped 态（后一 commit）。
- 姊妹仓 claude-sdlc-skills 领先 origin **7 commits**（parse_doc 七修 +
  --media-manifest）；本仓领先 origin 约 18 commits。**均未推送，推送时机问用户**。

## 三、runner 影响面（已探查完毕，照此拆）

### 直接删除

| 项 | 说明 |
|---|---|
| `runner/runner.mjs` | 外置 runner 本体（轮询 + spawn claude CLI） |
| `scripts/runner.sh` | runner 启动脚本 |

### 端点处置（关键：两个保留一个删）

| 端点 | 处置 | 依据 |
|---|---|---|
| `POST /api/tasks/next` | **保留** | verify-*.mjs / snapshot-keys.mjs 的 claimTask 模式在用（黑盒回归依赖） |
| `POST /api/tasks/recover-stale` | **保留** | verify-token.mjs 与 test_task_fencing 在用 |
| `GET /api/meta/task-rules`（`routers/meta.py`） | **删除** | 仅 runner.mjs 消费（DEFAULT_RULES 兜底下发面） |

### 联动修改

1. `scripts/verify-all.sh`：删 runner 暂停/恢复段（约 19-37 行的 `RUNNER_WAS_RUNNING`/
   `pgrep`/`nohup scripts/runner.sh`）——**agent 自动暂停段保留**。
2. `scripts/check_task_templates.py`：三处同步改**两处**——删 ② `runner.mjs promptFor`
   分支检查；③ 的 DEFAULT_RULES 对比随之调整（meta 端点删除后 task_rules.py 的
   payload 函数可删，但先查 `DRAFT_TYPES/NO_DRAFT_TYPES` 是否还有服务端消费面
   ——`task.approve` 草稿分流可能用，**保留数据、只删 runner 专属对照**）。
3. 注释更新：`agent/loop.py`、`agent/executor.py`、`agent/prompts.py` 里「与
   runner.mjs promptFor 同源」「两通道」等表述改为单通道口径（prompts.py 成为
   prompt 唯一权威）。
4. 测试：`tests/test_runtime.py` 的 claim(runner_name) 参数化测试是通道无关的
   （**保留**）；`tests/test_v5_clarify.py` 若有 runner 专属断言一并清。
5. 文档：`CLAUDE.md`（架构总览双通道→单通道、常用命令删 `scripts/runner.sh`、
   「三处同步」改「两处同步」、§8-4 表述）；`README.md`（runner 相关段落）。
6. `.gitignore` 的 `tasks/` 条目保留（进程内 agent 同样在用）。

## 四、执行顺序建议

1. 删 runner 本体 + runner.sh → 2. verify-all.sh 清理 → 3. check_task_templates.py
   改两处同步（跑通自证）→ 4. meta 路由删除（先 grep 全仓确认无他消费）→
   5. 注释/测试清理 → 6. CLAUDE.md/README → 7. 质量门 + 回归。

**每步质量门**：`uv run pyright && uv run ruff check src tests` + `uv run pytest`；
全部完成后 `scripts/verify-all.sh`（需服务启动：`./scripts/dev-log.sh` 后台；
结束后 pkill uvicorn）。

## 五、边界与红线

- 不动：`/api/tasks/next`、`/api/tasks/recover-stale`、task_repo.claim 双通道互斥
  语义（claim(fencing) 机制本身保留——verify 脚本还在走这个原子领取面）、
  `SDLC_RUNNER_*` 环境变量可一并清理但先 grep。
- runner.mjs 的 promptFor 文案与 prompts.py 的历史同源关系——删 runner 后无需迁移，
  prompts.py 已是权威；若发现 prompts.py 某分支文案滞后于 runner.mjs，**以 runner.mjs
  最后版本为准回流**（删除前先 diff 一次两边的任务类型分支集）。
- commit message 中文 + `Co-Authored-By: Claude Code <noreply@anthropic.com>`；
  `data/`、`static/` 不入库。
- 服务当前**已停止**；本仓 `.env` 已含飞书凭据（不入库）。
