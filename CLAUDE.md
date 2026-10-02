# CLAUDE.md

## 项目定位

SDLC 产物协作平台后端（Python/FastAPI，v3.0 起自 Java 迁移）。单服务单库（SQLite 单连接单写者），
任务队列 + 双执行通道（外置 runner.mjs spawn claude CLI / 进程内 openai-agents），产物走「草稿 → 人工采纳/发布」两态。
前端在姊妹仓 `sdlc-platform-web`。权威文档：`docs/DEV-PLAN.md`（API 契约）、`docs/DESIGN-v3-Python迁移.md`（映射 + 守卫对照）。

## 常用命令

```bash
scripts/dev.sh                 # uv sync + 启动 8080（cwd 必须是仓根：相对路径解析依赖它）
uv run pytest                  # 单元回归
scripts/verify-all.sh          # 黑盒全量回归（服务已启动）
scripts/backup.sh              # SQLite 快照
```

## 硬约束

- Python 3.12+ / uv；类型全量 strict（pyright）+ ruff；pytest 为单元回归，verify-*.mjs 为黑盒验收主力。
- SQLite 单连接 + RLock：`db.tx()` = 服务操作粒度事务（可重入 = 嵌套加入外层）；循环体内无数据库查询；SQL 全参数化 + 显式排序。
- schema 变更：schema.sql 加 `IF NOT EXISTS` 建表 + `migrations.py` 判列补列（幂等，八步惯例）。
- 分层单向：main → routers → services → repositories → models；routers 不碰仓储；services 不互调（跨域流程在 orchestration.py，挂在作答/发布入口之后）。
- 出参 camelCase（键序 = 契约序，见 schemas.py）；入参宽松绑定（dict + 服务层校验，文案与 Java 版逐字）；错误统一 `{error}`，乐观锁 409 带 conflicts。
- 前端产物 `static/` 为部署物不入库；数据 `data/` 不入库。

## 业务红线（改代码前必读，验收 = scripts/verify-*.mjs）

推送全量替换；重推按 seq+问题文本保留已答内容（P 编号漂移不携带）；发布终态锁（拒推/拒草稿/拒任务/拒作答）；
用例乐观锁 409+conflicts；sdlc-test 来源用例平台只读+评论；L2 编排只自动一次（人取消即终局）；
任务原子 claim 双通道互斥；agent 工具面不存在正式产物写入通道；GR-2/3/4 平台机械校验 + 放行关卡互认。
细节见 docs/DESIGN-v2* 与 DESIGN-v3 守卫对照清单。

## Git 惯例

commit message 中文，主题开头，如 `修复：…`、`P5：…`。`data/`、`static/`、`.venv/`、`backups/` 不入库。
