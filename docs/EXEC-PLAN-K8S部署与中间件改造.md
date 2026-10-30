# EXEC-PLAN：K8s 部署与中间件工程化改造

> 目标：平台（server + web 双仓）部署到内网服务器，形态 = Docker 容器 + K8s；
> 单机组件切换为公司标准工程组件：**SQLite → MySQL**、**内置任务队列 → RocketMQ**。
> 本文档 = 部署就绪度审计结论 + 依赖矩阵 + 改造清单 + 分阶段实施计划 + 验收标准。
> 依据：2026-10-28 对两仓源码的全量盘点（`main.py` / `config.py` / `db.py` / `repositories/*` /
> `runner/runner.mjs` / `scripts/*` / web 仓 `package.json` / `vite.config.js`），
> 以及公司部署规范样例仓 `hrsystem`（Dockerfile / k8s.tpl / groovy 流水线 / bootstrap 配置，见 §9）。

---

## §0 先回答：claude CLI 在架构里干什么

平台任务队列有**两条互斥执行通道**（同归 `task_repo.claim()` 原子领取，谁抢到谁执行）：

| 通道 | 实现 | 执行器 | 依赖 |
|---|---|---|---|
| 外置 Runner | `runner/runner.mjs` 轮询领任务 → `spawn claude -p <prompt> --permission-mode bypassPermissions --output-format json` | **Claude Code CLI headless**，跑 `claude-sdlc-skills` 仓里的技能管线（质检/标准文档/设计/门禁…），产物以「草稿」回传平台 | node ≥18、claude CLI 二进制 + 认证态、`~/IdeaProjects/claude-sdlc-skills` 仓、lark-cli（仅飞书来源质检任务） |
| 进程内 agent | `SDLC_AGENT_ENABLED=true` + openai-agents SDK（`agent/executor.py`） | server 进程内直连**任意 OpenAI 兼容端点**（如 DeepSeek），不依赖 claude CLI | `SDLC_AGENT_BASE_URL/MODEL/API_KEY` 三元组 + **skills 仓**（规范素材 + 确定性脚本，read_file/run_command 消费） |

**部署含义**：只用进程内通道 → 镜像/服务器不需要 claude CLI、认证态与 lark-cli（执行框架消除）；
但 **skills 仓（`claude-sdlc-skills`）是双通道共享依赖、不可消除**——进程内 agent 的 prompt 各任务
分支全量引用它（`read_file` 读 SKILL.md/references 规范模板 + `run_command` 跑 `parse_doc.py` 等
确定性脚本；`executor.py` 启动硬检查目录存在，缺失即任务全挂），须随镜像部署（见 §2.1/§3.5）。

> **✅ 已拍板（2026-10-28，同日修订；同日执行完毕）**：上服务器的是**完整能力**。初版拍板 = runner（claude CLI）
> 通道部署；**后修订为**：完整能力由「进程内 agent + 飞书 Python 接入」承载——
> 详见 `docs/DESIGN-飞书接入Python化.md`（其验收通过是 runner 下线前置）。
> **runner 已于 2026-10-28 下线**（本体 + task-rules 端点拆除，A/B 级评审扇出由进程内
> `spawn_reviewer` 工具承载）；上表外置 Runner 行与 §2.1 的 runner 依赖项均已消除
> ——但 **skills 仓除外**：它是进程内通道共享的内容依赖（素材库 + 脚本），保留并须进镜像。

---

## §1 现状盘点：单机假设清单（上 K8s 前会坏的点）

| # | 单机假设 | 代码位置 | 服务器/K8s 上的后果 |
|---|---|---|---|
| 1 | SQLite 单连接 + RLock + WAL | `db.py`（`check_same_thread=False`、PRAGMA、可重入 `tx()`） | 单写者进程内锁 → 无法多副本/多 worker；容器重建丢库（必须 PVC） |
| 2 | 助理会话是第二个 SQLite | `agent/assistant.py` `SqliteChatSession`（独立 `data/assistant.db`，模块级共享连接 + RLock） | 同上；且 `backup.sh` **不含它** |
| 3 | 任务队列 = DB 表轮询 | `task_repo.claim()` 条件 UPDATE + `AgentLoop` 进程内轮询（3s；`POST /api/tasks/next` 留作外部脚本原子领取面） | 无标准 MQ 的削峰/即时派发/消费可观测；换多执行器水平扩展受限 |
| 4 | cwd 相对路径 | `config.py` `resolve_paths`（`./data`）、`static_dir="static"` | uvicorn 必须以仓根为 cwd；容器 WORKDIR 必须钉死 |
| 5 | 本地文件系统三处 | `data/media/`（上传媒体，上限 64MB）、`tasks/<id>/out/`（source_import 服务端直读落库——**确定性管道红线**，不经 LLM 上下文）、`sdlc/<reqId>/`（任务工作区） | 三目录必须持久化且同进程可达（进程内 agent 与 server 同进程天然同路径；K8s 挂同一 PVC 即满足） |
| 6 | 前端产物 rsync 进 `static/` | web 仓 `npm run deploy`（`rsync -a --delete dist/ $SDLC_SERVER_STATIC_DIR`） | K8s 正解 = 多阶段构建打进 server 镜像（`base:'./'` 相对路径 + 同源 `/api`，无 CORS/域名假设，容器化友好） |
| 7 | 备份脚本假设本机 | `backup.sh`（`sqlite3 .backup`，需 sqlite3 CLI，只备 platform.db） | 容器内无常驻 shell；MySQL 化后换 mysqldump CronJob |
| 8 | 日志落本地文件 | `dev-log.sh` → `logs/server.log` | 容器正解 = stdout → K8s 采集 |
| 9 | 密钥走仓根 `.env` | `Settings(env_file=".env")`（pydantic-settings） | K8s = ConfigMap（非密）+ Secret（密钥）；`.env` 机制保留供本地开发 |
| 10 | 无登录、绑 0.0.0.0、内网互信 | `config.py`；写防护可选 `SDLC_WRITE_TOKEN` | 纯内网 IP 直访（已确认）→ K8s Service 即可，无 TLS/反代硬需求；写令牌仍建议启用 |

**现成有利条件**（盘点确认，不用新做）：`/api/health` 端点已有（probes 直接用）；SSE 已带
`X-Accel-Buffering: no`（反代兼容）；写防护 `SDLC_WRITE_TOKEN` 可选启用；
`verify-all.sh` 支持 `BASE` 参数指任意地址 + 自带进程内 agent 暂停/恢复（黑盒回归 = 现成验收门）。

---

## §2 依赖矩阵

### 2.1 运行时层

| 依赖 | 现状 | 处置 |
|---|---|---|
| Python ≥3.12 + uv | dev.sh `uv sync` | **仅需配置**（打进镜像） |
| MySQL 8.x | 无（SQLite） | **必须改造**（§3.1） |
| RocketMQ | 无（DB 轮询） | **必须改造**（§3.2） |
| node ≥18 | runner/verify 脚本 | **仅需配置**（CI 镜像；runner 已下线，仅 verify 脚本与 CI 需要） |
| claude CLI + 认证态 + lark-cli | 原 runner 通道专属 | ~~决策点~~ 已消除（§8-4 拍板后 runner 于 2026-10-28 下线；lark-cli 由进程内 Python 飞书客户端 `feishu_fetch` 替代） |
| **skills 仓（`claude-sdlc-skills`）** | **双通道共享**（进程内 `read_file` 规范素材 + `run_command` 确定性脚本，`executor.py` 启动硬检查） | **保留依赖，须进镜像**：`SDLC_AGENT_SKILLS_REPO` 指镜像内路径；版本 ≥ 2026-10-28（`parse_doc.py --media-manifest`，`feishu_fetch` 启动探测） |
| sqlite3 CLI | backup.sh | MySQL 化后移除 |
| OpenAI 兼容端点 + 余额 | 进程内 agent / 助理 | **仅需配置**（Secret） |

### 2.2 配置层（`SDLC_` 前缀，pydantic-settings，K8s 注入 env）

| 配置 | 去向 |
|---|---|
| `SDLC_AGENT_BASE_URL/_MODEL`、`SDLC_AGENT_ENABLED`、`SDLC_PLATFORM_URL`（改 K8s Service 名）、TZ | ConfigMap |
| `SDLC_AGENT_API_KEY`、`SDLC_WRITE_TOKEN`、MySQL 账号密码（新增 `SDLC_DB_DSN`）、飞书应用凭据（先共用 Askly 应用） | Secret（chmod 思路 = K8s 权限面，不进 git） |
| `SDLC_MEDIA_DIR`/`SDLC_AGENT_WORKSPACE_ROOT` | 改指 PVC 挂载点（如 `/data/...`）；~~SDLC_DB_PATH~~ 已退役（M2 起 `SDLC_DB_DSN`，见 §2.2 Secret 行） |
| `SDLC_AGENT_SKILLS_REPO` | 改指镜像内路径（如 `/app/skills`，构建期固化版本） |
| 需新增 `.env.example`（两仓通用配置文档化，防配置漂移） | **小改造** |

### 2.3 数据层

| 数据 | 体量 | 处置 |
|---|---|---|
| `data/platform.db`（18 张表，WAL） | 137M（含 media） | **迁移**：SQLite → MySQL（§6 脚本）；WAL 未 checkpoint，只信 `sqlite3 .backup` 产出 |
| `data/assistant.db`（chat_items） | 小 | **迁移**并入 MySQL（§3.3） |
| `data/media/` | 含于上 | rsync → PVC |
| `tasks/`、`sdlc/`（工作区） | 1.9M + 112M | rsync → PVC（在途需求的任务输出/工作区不断链） |
| `static/`（前端产物） | 13M | 不迁移——多阶段构建打进镜像 |

---

## §3 改造清单（每项：目标 / 为什么 / 风险）

### 3.1 数据层：SQLite → MySQL（最大改造，量化过了）

**改造面实测**：18 张表（`src/platform_sdlc/schema.sql`）、14 个仓储文件、**147 处 SQL**；
真正的 SQLite 方言点很少——`ON CONFLICT` ×3、`REPLACE INTO` ×1、`lastrowid` ×3（PyMySQL 同名兼容）、
`PRAGMA` ×2。**95% 差异集中在两点：占位符 `?` → `%s`、单连接 → 连接池。**

**设计：Database 适配层吸收差异，仓储层近零改动**
> **✅ 已实施（2026-10-29，M1~M4 批：cea2366/db57a25/05fa054/e66ebc1），实施修正三处**：
> ① 方言点实数 = ON CONFLICT ×2（非 ×3）、REPLACE INTO ×0；② 事务模型 = **RLock 保留 + 实例槽 _tx_conn**
> （本文 contextvar + RLock 退役方案被对抗审查推翻：MySQL 快照读不加锁，服务层 35 处「SELECT 守卫→UPDATE」
> 的 TOCTOU 防护依赖全程持锁，退役须逐处 SELECT FOR UPDATE 审计——replicas=1 下保留锁零损失）；
> ③ 实施新发现：字面 `%` ×7（三段式转换先 `%%` 后 `%s`）、MySQL 无 `CREATE INDEX IF NOT EXISTS`
> （拆分器容忍 1061）、information_schema 返回列名大写、派生表必须别名、CTE 名 `desc` 撞保留字、
> `by` 列保留字反引号、`SELECT ... WHERE` 无 FROM 需 `FROM DUAL`。测试 = testcontainers + CI service
> 直连双模式；本机 137M 库经 `scripts/migrate-sqlite-to-mysql.py` 导入对账全 ✅。

- 驱动：`PyMySQL` + `DBUtils PooledDB` 连接池（不引 SQLAlchemy/ORM——项目铁律是裸 SQL 全参数化 + 显式排序，仓储风格保持）。
- 占位符：`Database.execute/query_*` 入口统一 `sql.replace('?', '%s')` → **147 处仓储 SQL 零改动**
  （需先审计字面量中含 `?` 的 SQL——初查无，改造时复核）。
- 行对象：`DictCursor` 对齐 `sqlite3.Row` 的 `row["col"]` 访问。
- `tx()` 可重入事务（Spring REQUIRED 语义）：改为 **contextvar 绑定事务连接 + 深度计数**
  （嵌套加入外层；跨线程并发控制由 InnoDB 行锁接管，进程内 RLock 退役）。
  注意路由层 `asyncio.to_thread(svc.xxx)` 一次调用一个线程、tx 内 repo 调用同线程，contextvar 模型成立。
- 手改点：`ON CONFLICT` ×3 → `ON DUPLICATE KEY UPDATE`；`REPLACE INTO`（MySQL 同语法，直接用）。
- `schema.sql` 重写 MySQL 方言（`AUTOINCREMENT`→`AUTO_INCREMENT BIGINT`、TEXT→VARCHAR/TEXT 按长
  utf8mb4、补 KEY/INDEX）；`migrations.py` 判列补列改查 `information_schema`（幂等惯例保持）。
- `Settings`：`db_path` → 新增 `db_dsn`（`mysql://user:pass@host:3306/sdlc`）；`resolve_paths` 相应调整。
- 时区：容器统一 `TZ=Asia/Shanghai`（`Times.now()` 本地时间字符串、字典序排序依赖格式不依赖时区，但跨环境必须一致）。

**风险**：① 双事务语义细节（隐式提交、隔离级别默认 RR 对「读-改-写全程持锁」的等价性——
claim/finish 的条件 UPDATE 在 InnoDB 下原子性成立，红线语义保住）；② 测试改造（见 §4 阶段 2）；
③ 时间字段字符串写入 DATETIME 的边界（毫秒截断/零值）需回归覆盖。

### 3.2 任务队列 → RocketMQ（架构决策先行）

**推荐架构：MySQL 任务表保持权威状态机，RocketMQ 只做派发通知（业界 DB-as-truth + MQ 派发标准形态）。**
理由：任务状态机是业务红线的载体——「原子 claim 双通道互斥」「finish 仅 running 可回报 + attempt
fencing」「L2 只自动一次（`exists_by_type` 直查）」「终态锁拒任务」「recover-stale 归属回收」——
全部以 DB 条件 UPDATE 为实现机制。状态入 MQ 则这些红线全部重写，得不偿失。

语义映射：

| 现状机制 | 目标机制 |
|---|---|
| `POST /api/tasks/next` 轮询 claim（外部脚本用） | 保留（兼容 + 兜底） |
| `AgentLoop` 3s 轮询 | 订阅 `task.created` 消息即时唤醒 claim + 30s 轮询兜底（MQ 丢消息不丢任务） |
| `create_guarded` 建任务 | 事务提交后发 `task.created`（失败仅日志，轮询兜底——起步不做本地消息表） |
| finish / retry / fencing / recover-stale | **不变**（仍是 DB 条件 UPDATE） |

**⚠ 高风险（hrsystem 构件已实锤加重）**：公司 MQ 走 Apache RocketMQ **4.x remoting 协议**
（hrsystem 用 `rocketmq-spring-boot-starter` 2.3.1），而 **4.x 没有官方 Python 客户端**（官方多语言
gRPC 客户端只支持 5.x proxy）。Python 接入的现实路径：① 公司 MQ 若暴露 HTTP/网关端点（问运维）；
② 集群若有 5.x gRPC proxy → 官方 `rocketmq-clients`；③ Java sidecar 消费网关；④ C++ client 的
Python 绑定（安装地狱，不推荐）。
**专业意见（供拍板）**：本平台是内部工具量级（分钟级任务、非秒级洪峰），MySQL 条件 UPDATE 轮询在
InnoDB 下原子性成立，「DB 权威 + 轮询」的收益损失很小，而 Python 接 MQ 成本高——建议 MQ 接入
**降级为可选优化项**（等公司侧出现 HTTP/5.x 端点再做）。此项不拍板不排期。

### 3.3 助理会话库并入 MySQL

`SqliteChatSession`（`chat_items` 单表）→ MySQL 同名表 + 连接池；模块级共享单连接/RLock/`close_shared_session_db` 退役。
顺带修掉盘点发现的缺陷：**backup.sh 从不备 assistant.db**（并入 MySQL 后该问题消失）。

### 3.4 文件存储 PVC 化

- PVC `sdlc-data`（建议 50Gi 起，现用量约 250M）：`data/media`、`tasks/`、`sdlc/` 三目录经
  `SDLC_MEDIA_DIR`/`SDLC_AGENT_WORKSPACE_ROOT`/workspace env 指到 `/data/` 挂载点。
- **红线**：source_import 完成回调服务端直读 `tasks/<task_id>/out/doc.json` → 进程内 agent 与
  server 同进程天然同路径，K8s 挂同一 PVC 即满足（RWO 单副本无共享诉求；storageclass §8-1 确认后定）。
- `static/` → 多阶段构建打进镜像（web `dist` → server 镜像 `/app/static`），发布即发版，无运行时同步。

### 3.5 镜像与部署配套（全新新增，非业务代码改动）

- `Dockerfile.server`（多阶段）：stage1 `node:20` build web（`npm ci && npm run build`）→ stage2
  `python:3.12-slim` + uv sync + `COPY --from=stage1` dist → `static/` + **skills 仓（clone/COPY
  `claude-sdlc-skills` → `/app/skills`，构建期固化版本 ≥ 2026-10-28）**；WORKDIR 钉仓根；非 root 用户。
- `.dockerignore`（data/ tasks/ sdlc/ logs/ backups/ .venv .git static/）。
- K8s manifests：`Deployment sdlc-server`（**replicas=1 硬约束**——MySQL 虽消除单写者，但 AgentLoop
  进程内执行、共享 workspace、单 uvicorn worker 模型仍在；水平扩列远期）、
  `Service`（内网直访）、`PVC`、`ConfigMap`/`Secret`、~~`CronJob sdlc-backup`~~（✅ 2026-10-29 拍板移除：备份走公司 DB 平台，见 §3.6）、probes（`/api/health`，
  readiness 加 MySQL 连通性）、`terminationGracePeriodSeconds`（SSE 流与在跑任务软着陆——超时任务由
  recover-stale 兜底回收）。
- compose 文件（本地/单机迭代载体，MySQL/RocketMQ 容器一起拉起）。

### 3.6 备份与日志

- 备份：~~mysqldump CronJob → PVC/对象存储~~（**✅ 2026-10-29 拍板：不自建备份 CronJob，依赖公司
  MySQL 平台的备份/恢复能力**，§8-3 同步闭环）。`scripts/backup.sh`（mysqldump 版）保留为本地开发
  自助快照工具。**边界提示**：PVC 上的 `data/media`、`tasks/`、`sdlc/` 工作区是文件数据，不在 DB
  备份覆盖内——需恢复时靠 PVC 快照/手工 rsync 自助，runbook 里记一笔。
- 日志：uvicorn 全部 stdout（K8s 采集接公司体系）；`logs/` 目录退役。

### 3.7 验证脚本适配

`verify-all.sh` 已改为调 `/api/agent/pause` 自动暂停/恢复进程内 agent（runner 段随下线删除）；
`BASE` 参数支持远程地址——容器/K8s 下无需改。

---

## §4 分阶段实施计划（路线 A：先上 K8s 可用，中间件改造随后，每阶段独立可上线）

> 路线 B（全部改造完一次上）可作为压缩选项，但不推荐：上线窗口晚、风险耦合。

### 阶段 1：容器化 + K8s 上线（原架构原样，SQLite + PVC）——约 2~3 人日

> **✅ 已实施（2026-10-29，批次 A~F；实际顺序倒置——阶段 2 MySQL 先收官，容器化在 MySQL 基线上进行）**：
> 交付物 = `Dockerfile.server`（多阶段：node 构建 web → python:3.12-slim + uv 两步 sync + skills 仓固化
> /opt/app/skills + tzdata/curl/jq + 非 root + `--timeout-graceful-shutdown 30`）+ `.dockerignore` +
> `deploy/build.sh`（两仓 rsync 暂存 .build/ + REVISION 版本固化 + parse_doc --media-manifest 构建期门禁 +
> SWR 代理前置探测）+ `deploy/k8s.tpl`（replicas=1 写死 / Recreate / httpGet 三探针 / initContainer 建目录 /
> envFrom ConfigMap+Secret / securityContext 分层防 init 冲突）+ `deploy/k8s/` PVC·ConfigMap·Secret 三静态件 +
> 仓根 `aliprod.groovy`（自渲染 sed+apply，不依赖共享库——平台侧出 Python 变体后可切）+
> `compose/server.verify.yml`（本地容器验证，独立项目名 sdlc-verify 与 dev 栈隔离）+ `docs/部署runbook.md` +
> readiness 端点（`/api/health/ready` 池租借 ping 不取 RLock；`/api/health` 保持静态作 liveness——204 测试含解耦回归）。
> 方案经全新上下文对抗式审查（1 P0 + 4 P1 全吸收：init×runAsNonRoot 冲突 / 镜像补 curl / compose 补
> AGENT_ENABLED / verify-all 的 SDLC_VERIFY_AGENT 分支 pause 矛盾修正 / SSE 收尾参数）。
> **容器内首验实录**：单容器冒烟全过（health/ready 双 200、TZ +08:00、非 root uid=1000、curl/jq 在位、
> skills REVISION=49ef0b3）；verify-all **335 PASS / 0 FAIL**（含 verify-token 自建实例，DSN 走 13306，
> tasks/ 绑定挂载对齐宿主夹具面）；**verify-agent 真模型端到端通过（M6 遗留正式补掉**：source_import
> 9 块落库 → quality_check 14 澄清/锚点 46/53 → digest 全链）。
>
> **⚠ 2026-10-30 目标收窄（分工调整）**：K8s 编排/集群配置全部移交运维，本仓交付边界 = 自带配置的
> Docker 镜像（配置构建期 baked：`deploy/env.image` → `/opt/app/.env`，pydantic-settings 直读；
> 密钥进镜像层为内网开发环境既定取舍）。**K8s 交付物同日撤出仓**：`deploy/k8s/` 三静态件、`deploy/k8s.tpl`、
> 仓根 `aliprod.groovy`、`deploy/prestop.py` 删除（Dockerfile 同步去 prestop COPY；探针/单副本/Recreate/
> 卷等运行契约浓缩进 runbook §5 交接表）。`deploy/build.sh` 增配置暂存 + DSN 非空构建期门禁；
> `compose/server.verify.yml` 改 baked 形态验证（栈不再注入 DSN/AGENT_ENABLED）。原 §7 运维确认清单
> 与上线动作归运维侧。镜像推送（PUSH=1）能力保留，待运维定 SWR region 后启用。

1. `Dockerfile.server`(+`.dockerignore`) + compose 本地起 → `verify-all.sh` 全绿（容器内首验）。
2. ~~K8s manifests（Deployment/PVC/Service/ConfigMap/Secret/probes）→ 内网集群上线~~（2026-10-30 归运维，交付物已撤出仓）。
3. 数据：全新起库（✅ §6），PVC 空挂即可，无迁移步骤。
4. 任务通道：**进程内 agent（唯一通道，✅ §0/§8-4 拍板）**——skills 仓 `claude-sdlc-skills` 在
   `Dockerfile.server` 构建期固化进镜像（版本 ≥ 2026-10-28），`SDLC_AGENT_SKILLS_REPO` 指镜像内
   路径（如 `/app/skills`）；飞书应用凭据（先共用 Askly）走 Secret env；容器内首验含
   `feishu_fetch` 版本探测通过。
5. **验收门**：内网 IP `curl /api/health` OK；`verify-all.sh BASE=<内网地址>` 全绿；浏览器走通一条业务流。

### 阶段 2：SQLite → MySQL——约 5~8 人日（含测试改造）**✅ 2026-10-29 完成（M1 预备/M2 切换/M3 assistant 并入/M4 本机导入；验收门全过：202 单测 MySQL 后端全绿 + verify-all 12 黑盒全绿 + 守护索引首启建成 + 18 表对账）**

1. `db.py` 适配层重写（连接池/contextvar 事务/占位符转换，§3.1 设计）；`schema.sql` + `migrations.py` MySQL 化。
2. 方言点手改（ON CONFLICT ×3 等）+ 全库回归：pytest（策略见下）+ compose 双件套（server+MySQL 容器）跑 `verify-all.sh`。
3. 数据迁移脚本（§6）在本机数据集上演练 → 生产切换窗口执行。
4. **测试策略（决策点，默认推荐）**：单方言 MySQL + `testcontainers-python` 起真库跑全量 pytest
   （CI 需 docker）；不保双方言（长期税 > 短期痛）。
5. **验收门**：pytest 全绿（MySQL 后端）；verify-all 全绿；迁移后数据抽样比对（需求卡数/块数/任务数/用例数一致）。

### 阶段 3：RocketMQ 派发接入——约 3~5 人日（前置：§8-2 客户端选型落定）

1. 生产者：`create_guarded` 提交后发 `task.created`；消费者：AgentLoop 订阅唤醒 + 轮询兜底。
2. claim/finish/fencing/recover 全不动；补消费幂等与堆积告警接入公司监控。
3. **验收门**：建任务 → MQ 控制台消息可见 → 执行完成；杀 MQ（演练）→ 轮询兜底仍领到任务（红线：不丢任务）。

### 阶段 4：收尾——约 2 人日

assistant.db 并入 MySQL；`.env.example`；部署 runbook 文档（docs/）+ 验收总门（§7）全量过一遍。

---

## §5 目标 K8s 拓扑

```
Namespace: sdlc
┌────────────────────────────────────────────┐   外部标准组件（公司平台）
│ Deployment sdlc-server (replicas=1)        │  ┌─────────────┐
│   container: uvicorn :8888 (probe /api/health)│  │ MySQL 8.x   │◄── Secret 账号
│   ├─ PVC sdlc-data (media/tasks/sdlc 共享) │  └─────────────┘
│   ├─ ConfigMap (SDLC_AGENT_* 非密/TZ/URL)  │  ┌─────────────┐
│   └─ Secret (API_KEY/WRITE_TOKEN/DB DSN)   │  │ RocketMQ    │◄── task.created
│   （skills 仓固化进镜像 /app/skills）       │  └─────────────┘
│ （备份不自建 CronJob——走公司 DB 平台，2026-10-29 拍板）│
│ Service sdlc-server ← 内网 IP 直访（无 TLS 需求）
└────────────────────────────────────────────┘
```

---

## §6 数据起点：全新起库（✅ 已拍板，无历史数据迁移）

服务器全新起库，不迁移本机现网数据（137M data + 112M 工作区留在本机开发环境继续用）：
- 阶段 1（SQLite）：首启自动建库 + 幂等迁移，零操作。
- 阶段 2（MySQL）：`schema.sql` 全量建表 + `migrations.py` 首启判列补列即可；原 SQLite→MySQL 迁移
  脚本与停写窗口方案作废（若未来要迁：`sqlite3 .backup` 快照 + 按依赖序 18 表导入的思路仍可复用
  ——绝不直接 cp WAL 库文件）。

---

## §7 验收总门（每次上线 + 每季度回归）

> **阶段 2 验收实录（2026-10-29，M6 批）**：① pytest 202/202（MySQL 后端）；② verify-all 12 黑盒全绿
> （MySQL 实例 + 导入真数据，全程 11s 无劣化）；③ 业务流黑盒覆盖（verify-*.mjs 建卡→质检→澄清→发布→
> 设计→门禁免模型全链）；④ verify-agent 真模型端到端未跑（需模型余额——阶段 1 容器化首验时补）；
> ⑤ 备份/恢复演练过（mysqldump --single-transaction 1.1M gzip → 临时库恢复四表 COUNT 一致）；
> 部署核对项：8.0.40 / utf8mb4_0900_ai_ci / STRICT_TRANS_TABLES / wait_timeout 28800 / max_allowed_packet 64M 全达标。

1. `uv run pytest` 全绿（MySQL 后端）；
2. `scripts/verify-all.sh BASE=<内网地址>` 全绿（自带进程内 agent 暂停/恢复）；
3. 浏览器走通完整业务流：建卡 → source_import → 质检 → 澄清答复 → 发布 → 设计 → 门禁；
4. `verify-agent.mjs` / `verify-assistant.mjs` 通过（需模型余额）；
5. K8s 韧性演练：pod 删除重建数据无损（备份不自建——2026-10-29 拍板走公司 DB 平台，恢复保障在平台侧）；
6. （阶段 3 后）MQ 断连演练：轮询兜底不丢任务。

任一不满足 = 上线未完成。失败判据：任一 verify 非零退出 / SPA 白屏 / 业务流断链 / 数据计数不一致。

---

## §8 待确认问题清单（2026-10-28 二轮：hrsystem 构件已答一半，✅=已确认）

1. **K8s 集群**：✅ 阿里云 ACK，Jenkins 共享库 + `k8s.tpl` 模板部署（hrsystem 样例 namespace `hrjy`）；
   ✅ **PVC 可用**（storageclass 具体名部署时向运维确认；默认按 RWO + podAffinity 同节点设计）。
   仍待确认：SDLC namespace 归属、CronJob 权限。
2. **RocketMQ**（仍悬置，不阻塞阶段 1-2）：✅ 公司走 Apache 4.x；仍待确认 HTTP/5.x 端点 +
   是否接受 §3.2「降级为可选优化」。
3. **MySQL**：✅ 公司标准组件（账号/地址经 Apollo 注入）。✅ **备份已拍板（2026-10-29）：走公司
   DB 平台，不自建 CronJob**（PVC 文件数据不在 DB 备份内，边界见 §3.6）。
   仍待确认：建库申请流程、Python Pod 直连网络策略。
4. **任务通道**：✅ **完整能力**（2026-10-28 修订，同日执行完毕）：飞书能力改由进程内 agent 的
   Python 接入承载（`DESIGN-飞书接入Python化.md`，凭据先共用 Askly 应用）；runner 已于 2026-10-28
   下线（A/B 级评审扇出由进程内 spawn_reviewer 工具承载），容器化只部署 server（进程内通道）。
5. **数据起点**：✅ **全新起库**（§6）。
6. **镜像与 CI**：✅ SWR（组织 `hlyz` + `swr-secret`）；CI **暂跳过**——镜像先本地构建推送
   （`deploy/build.sh`），流水线等运维确认后补。
7. **时区**：✅ `Asia/Shanghai`。
8. **配置中心**：✅ 接受起步 ConfigMap/Secret 直配，不接 Apollo/Nacos。

---

## §9 hrsystem 构件对照（公司部署规范 → 本计划的可复用与差异）

**hrsystem 栈**：Java 21 + Spring Boot 2.7.18（DDD 多模块 Maven）；MySQL(Druid+MyBatis-Plus)、
RocketMQ 4.x、Nacos 1.4.4(发现)、Apollo(配置)、Redisson、xxl-job、OTel 注入；华为云 SWR 镜像；
阿里云 ACK（Jenkins 共享库 + k8s.tpl）；日志 = hostPath `/var/lib/containerd/applogs` + `logType: app`
label 宿主机采集。

| 公司规范（hrsystem 实样） | SDLC 处置 |
|---|---|
| `k8s.tpl` 模板（{{APP_NAME}}/{{IMAGE_URL}} 占位 + groovy 渲染） | **复用模式**，产出 SDLC 版 tpl：readiness 换 `httpGet /api/health`（比 tcpSocket 准，端点现成）；**加 PVC**（hrsystem 无状态无卷）；单副本去掉 podAntiAffinity |
| SWR 镜像组织 `hlyz` + `swr-secret` | 直接沿用 |
| 基础镜像走 SWR 的 docker.io 代理 | Python 侧 = `swr…/docker.io/python:3.12-slim` + node build stage（同样走代理拉 `node:20`）——内网集群拉外网镜像的既有通路 |
| preStop 先 Nacos 下线再杀 pod | SDLC 无 Nacos 注册——preStop 改为「停领新任务（agent pause）+ 等在跑任务软着陆」，`terminationGracePeriodSeconds` 对齐 120s |
| tcpSocket 三段探针 | 换 httpGet（SDLC 有 `/api/health`） |
| 日志 hostPath applogs + logType label | 对齐：容器日志写 stdout 或挂 hostPath applogs（二选一问运维采集方式） |
| OTel annotation 注入 java agent | Python 亦有 otel operator 注入方案；**可选不阻塞** |
| `nodeSelector: node.apptype: web` | 沿用 |
| 凭证全在 Apollo/Nacos | 起步 ConfigMap/Secret（§8-8 决策点） |
| Jenkins `deploy_aliack_cluster_jdk21(map)` | JDK 专用函数；Python 需共享库加变体或走 GitLab CI 模板（§8-6） |

---

*盘点与计划生成：2026-10-28；改造面数字（18 表 / 14 仓储 / 147 SQL / 方言点 3+1+3）为当日实测。*
