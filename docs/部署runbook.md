# SDLC 平台镜像交付说明（原 K8s 部署 runbook）

> 2026-10-30 目标收窄：**K8s 编排/集群配置归运维**，本仓交付边界 = 一个自带配置的 Docker 镜像
> （`sdlc-platform-server:local`；需推送时 `PUSH=1` 待运维定 SWR region 后一条命令）。
> 配置走**构建期 baked**：`deploy/env.image` → `/opt/app/.env`（pydantic-settings cwd 直读，
> 运行期环境变量仍可覆盖文件值）。
> 配套件：`Dockerfile` / `deploy/build.sh` / `deploy/env.image.example` / `compose/server.verify.yml`。
> 计划与决策记录见 `docs/EXEC-PLAN-K8S部署与中间件改造.md`。

## 1. 交付物形态与硬约束

- 形态：**纯后端 API 单容器**（uvicorn :8888；前端 dist 不在镜像——由部署侧静态托管，与 `/api`
  同源反代，平台无 CORS 中间件）+ 单卷 `/data`（media/tasks/sdlc 工作区文件面）+ MySQL
  （公司标准组件，Pod 直连）。**配置已 baked 进镜像，运行侧无需注入任何 env**（覆盖排障除外）。
- **replicas=1 不可改**：db 层 RLock 进程内全局串行（「SELECT 守卫→UPDATE」TOCTOU 防护依赖全程持锁）+
  AgentLoop 进程内任务执行——双副本即双进程，任务原子 claim 单通道与全部守卫失效。
- 探针分工：`/api/health` 静态（liveness/startup——DB 抖动只摘流不重启）；`/api/health/ready` 含 MySQL
  连通性（readiness，DB 不可达即摘除出 Service）。ready 探测不取全局 RLock（池租借语义），长事务期间不误判。
- 日志：容器 stdout。
- 时区 Asia/Shanghai 已固化进镜像（tzdata + ENV + /etc/localtime 三层；容器内 `date` 非 +08:00 = 立即排查）。

## 2. 构建前置（构建机 = 本地开发机）

- docker / rsync / git / uv / bash（`deploy/build.sh` 与 verify 脚本面）
- skills 仓就位：`~/IdeaProjects/claude-sdlc-skills`（技能仓，版本须 ≥ 2026-10-28 即
  `parse_doc.py --media-manifest`——build.sh 有构建期门禁）
- 配置文件：`cp deploy/env.image.example deploy/env.image` 填真值（gitignore 不入库；DSN 密码特殊字符
  必须 URL 百分号编码）。⚠ 密钥（DSN 密码/API_KEY/写令牌/飞书 secret）将进镜像层——内网开发环境既定
  取舍，**镜像勿推公网仓库**。

## 3. 构建

```bash
deploy/build.sh                                  # 只构建：产 <REGISTRY>/hlyz/sdlc-platform-server:<tag> + local tag
ENV_FILE=deploy/env.verify deploy/build.sh       # 验证栈专用配置（见 §4）
REGISTRY=… ORG=… PUSH=1 deploy/build.sh          # 构建并推 SWR（需先 docker login；region 待运维定）
```

- build.sh 自动完成：skills 仓 rsync 暂存（`.build/`，已 gitignore）→ skills 版本写
  `REVISION` 进镜像 → `parse_doc --media-manifest` 门禁 → **配置暂存 + DSN 非空门禁（空 = 构建期红）**
  → 基础镜像代理可达性探测 → build →（PUSH=1 时）push。
- **skills 版本固化与排障**：`docker run --rm <image> cat /opt/app/skills/REVISION` 即得构建时固化的
  commit 与主题；升级 skills 仓后重新 build 即更新（镜像不可变，无运行时漂移）。
- 已知耦合：feishu fetch 用镜像内 venv python 调 skills 仓脚本——skills 脚本依赖必须 ⊆ 主仓依赖
  （当前纯 stdlib 成立；若 skills 将来引第三方库，需同步进 pyproject）。

## 4. 容器内验证（每次交付必过；任一不满足 = 交付未完成）

```bash
# ⓪ 验证配置打镜像（DSN 指向 compose 栈内 mysql 服务名）
cp deploy/env.verify.example deploy/env.verify && ENV_FILE=deploy/env.verify deploy/build.sh
# ① 起栈（agent 三元组密钥经宿主 env 透传；栈不注入 DSN/AGENT_ENABLED——起栈即验证 baked 通道生效）
SERVER_IMAGE=sdlc-platform-server:local SDLC_AGENT_API_KEY=<key> \
  docker compose -f compose/server.verify.yml up -d --wait      # --wait 后 curl /api/health 就绪再跑 ②
# ② 黑盒全量（verify-all 自带 agent pause/resume 防竞态）
SDLC_DB_DSN=mysql://root:sdlc@127.0.0.1:13306/sdlc bash scripts/verify-all.sh http://127.0.0.1:8888
# ③ 真模型冒烟（需模型余额）
SDLC_AGENT_BASE_URL=… SDLC_AGENT_MODEL=… SDLC_AGENT_API_KEY=… node scripts/verify-agent.mjs http://127.0.0.1:8888
# ④ 清场
docker compose -f compose/server.verify.yml down -v
```

验收门：`/api/health` 与 `/api/health/ready` 双 200（DSN 未注入仍 ready = 配置确在镜像里）；verify-all
12 黑盒全绿；verify-agent 端到端过；容器内 `date` 为 +08:00。

## 5. 运维交接：镜像运行契约

| 项 | 契约 |
|---|---|
| 端口 | 8888（单进程，`--timeout-graceful-shutdown 30`） |
| 卷 | 单卷挂 `/data`（RWO 即可）；`/data/media`、`/data/ws` 目录须存在（镜像层已建，挂空卷后被覆盖需运维保证） |
| 探针 | liveness/startup = `GET /api/health`（静态）；readiness = `GET /api/health/ready`（含 MySQL 连通） |
| 副本 | **replicas=1 硬约束**（见 §1）；发布策略建议 Recreate（RWO 单卷 + 双进程即破红线），停机窗口最坏 2~3 分钟 |
| 配置 | 已 baked 进镜像（`/opt/app/.env`），运行侧零注入；环境变量可临时覆盖（优先级高于文件） |
| 前端 | dist **另行静态部署**（nginx 等），须与 `/api` 同源反代——平台无 CORS 中间件；跨域直连需先给平台加 CORSMiddleware |
| 数据库 | MySQL 8（utf8mb4_0900_ai_ci / STRICT_TRANS_TABLES）；首启自动建表（schema.sql 全量 + 幂等迁移），需可写 DDL |
| agent | 需模型端点（OpenAI 兼容）网络可达 + 余额；`curl -X POST /api/agent/pause` 可停领不停服 |
| 时区 | 镜像内已固化 Asia/Shanghai |
| 升级/回滚 | 换镜像 tag 重新部署；启动期幂等迁移自动补列（只加不删——回滚只回滚到 schema 兼容版本） |

移交前需运维确认：namespace 归属、storageClass 名（reclaimPolicy 建议 Retain——Delete 下 PVC 误删即文件
数据全失）、日志采集方式（stdout vs hostPath）、MySQL 建库流程 + Pod 直连网络策略、镜像仓库 org/region
终值（cn-north-4 vs cn-east-3 两见）与拉取凭据。发布窗口选业务空闲时段（Recreate 停机；在跑 agent 任务由
expire_stale + 300s sweeper 置 failed 可重试，SSE 聊天流客户端重连）。

## 6. 数据边界与备份（决策记录）

- **不自具备份 CronJob**（2026-10-29 拍板）：MySQL 库依赖公司 DB 平台的备份/恢复能力，恢复演练责任在平台侧。
- **`/data` 文件面不在 DB 备份覆盖内**：`media/`（上传媒体）、`tasks/<id>/out/`（source_import 服务端
  直读产物——确定性管道红线）、`sdlc/<reqId>/`（任务工作区）。需恢复时靠卷快照 / 手工 rsync 自助；
  定期（建议每月）留存关键需求的工作区。
- 本地开发库快照工具 `scripts/backup.sh`（mysqldump）保留为开发自助面，与线上无关。

## 7. 已知限制

- 发布必停机（Recreate 窗口最坏 2~3 分钟），选业务空闲窗口。
- 发布中断在跑 agent 任务与 SSE 流：前者由 expire_stale + sweeper 300s 置 failed 可重试，后者客户端重连。
- readiness 最坏判负延迟由 `connect_timeout=10s` 决定（DB 整机失联时探针 3s 超时先摘流，方向正确）。
- 媒体上传上限 64MB（`media_max_bytes`）；内网直访无 ingress 时无 body size 中间层顾虑。
- 单 worker 模型下长阻塞 DB 调用（read_timeout=60s 兜底）会拖慢并发的其他请求——属 RLock 串行既定代价。
- 配置 baked 意味着**换配置/换密钥须重打镜像**（密钥进镜像层的取舍已拍板；勿推公网仓库）。
