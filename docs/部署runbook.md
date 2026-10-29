# SDLC 平台部署 runbook（K8s · 阿里云 ACK）

> EXEC-PLAN-K8s 阶段 1 交付物。配套件：`Dockerfile.server` / `deploy/build.sh` / `deploy/k8s.tpl` /
> `deploy/k8s/{pvc,configmap,secret}.yaml` / `aliprod.groovy` / `compose/server.verify.yml`。
> 机器可执行流程以本文为权威；EXEC-PLAN（`docs/EXEC-PLAN-K8S部署与中间件改造.md`）为计划与决策记录。

## 1. 架构与硬约束

- 形态：单 Deployment 单容器（uvicorn 单进程 :8888）+ 单 PVC `/data` + MySQL（公司标准组件，Pod 直连）。
- **replicas=1 不可改**：db 层 RLock 进程内全局串行（「SELECT 守卫→UPDATE」TOCTOU 防护依赖全程持锁）+
  AgentLoop 进程内任务执行——双副本即双进程，任务原子 claim 单通道与全部守卫失效。
- **strategy=Recreate**：RWO PVC 单副本下 RollingUpdate 新旧并存挂不上卷；停机窗口 = 旧 pod 终止到新 pod
  Ready（最坏约 2~3 分钟）。**发布请选无人用平台的窗口**；在跑 agent 任务与 SSE 聊天流会被中断，
  由 uvicorn `--timeout-graceful-shutdown 30` 收尾 + 启动期 expire_stale / `_stale_sweeper` 300s 兜底回收。
- 探针分工：`/api/health` 静态（liveness/startup——DB 抖动只摘流不重启）；`/api/health/ready` 含 MySQL
  连通性（readiness，DB 不可达即摘除出 Service）。ready 探测不取全局 RLock（池租借语义），长事务期间不误判。
- 日志：容器 stdout（`logType: app` 标签已打；采集方式若定 hostPath applogs 再补卷，见 §7）。

## 2. 前置条件（构建机 = 本地开发机）

- docker / rsync / git / node ≥18 / uv / bash（`deploy/build.sh` 与 verify 脚本面）
- 两仓就位：`~/IdeaProjects/sdlc-platform-web`（前端源码）、`~/IdeaProjects/claude-sdlc-skills`
  （技能仓，版本须 ≥ 2026-10-28 即 `parse_doc.py --media-manifest`——build.sh 有构建期门禁）
- SWR 推送凭据：`docker login swr.<region>.myhuaweicloud.com`（组织 `hlyz`）
- 首次 apply 前注意：本机无 kubectl 集群连接时 `--dry-run=client` 也打 API server——模板结构校验以
  YAML 解析为准，真正的 schema 校验发生在集群首 apply。

## 3. 一次性准备（新环境首部署，均手动 kubectl）

```bash
NS=<namespace>                       # 归属待运维确认（§7）
kubectl create namespace $NS

# ① PVC（storageClassName 待运维确认；确认 StorageClass reclaimPolicy=Retain）
kubectl -n $NS apply -f deploy/k8s/pvc.yaml

# ② ConfigMap（非密；先填好 SDLC_AGENT_BASE_URL/MODEL 与飞书 APP_ID 再 apply）
kubectl -n $NS apply -f deploy/k8s/configmap.yaml

# ③ Secret（create 不 apply——防占位值覆盖真值；DSN 密码特殊字符必须 URL 百分号编码）
kubectl -n $NS create secret generic sdlc-platform-secret \
  --from-literal=SDLC_DB_DSN='mysql://user:<URL编码后的密码>@<host>:3306/sdlc' \
  --from-literal=SDLC_AGENT_API_KEY='<key>' \
  --from-literal=SDLC_WRITE_TOKEN='<写防护令牌>' \
  --from-literal=SDLC_FEISHU_APP_SECRET='<secret>'

# ④ 镜像拉取凭据（公司既有 swr-secret；若无则按运维流程在 namespace 内创建）
```

MySQL 库：按公司建库流程申请（§7），DSN 进上述 Secret；首启自动建表（schema.sql 全量 + 幂等迁移）。

## 4. 构建与推送

```bash
deploy/build.sh                                  # 只构建：产 <REGISTRY>/hlyz/sdlc-platform-server:<tag> + local tag
REGISTRY=… ORG=… PUSH=1 deploy/build.sh          # 构建并推 SWR（tag 默认 日期-短rev）
```

- build.sh 自动完成：web/skills 两仓 rsync 暂存（`.build/`，已 gitignore）→ skills 版本写
  `REVISION` 进镜像 → `parse_doc --media-manifest` 门禁 → 基础镜像代理可达性探测 → build → push。
- **skills 版本固化与排障**：`kubectl exec <pod> -- cat /opt/app/skills/REVISION` 即得构建时固化的
  commit 与主题；升级 skills 仓后重新 build 即更新（镜像不可变，无运行时漂移）。
- 已知耦合：feishu fetch 用镜像内 venv python 调 skills 仓脚本——skills 脚本依赖必须 ⊆ 主仓依赖
  （当前纯 stdlib 成立；若 skills 将来引第三方库，需同步进 pyproject）。

## 5. 部署

- **Jenkins**：任务脚本 = 仓根 `aliprod.groovy`（自渲染 sed + kubectl apply，不依赖共享库——平台侧
  出 Python/带 PVC 变体后可切换，见文件头注释）。参数 NAMESPACE / IMAGE_TAG（抄 build.sh 输出）。
- **手工等价通道**：
  ```bash
  sed -e 's|{{APP_NAME}}|sdlc-platform|g' -e 's|{{NAMESPACE}}|<ns>|g' -e 's|{{PORT}}|8888|g' \
      -e 's|{{IMAGE_URL}}|swr.<region>.myhuaweicloud.com/hlyz/sdlc-platform-server|g' \
      -e 's|{{IMAGE_TAG}}|<tag>|g' deploy/k8s.tpl | kubectl apply -n <ns> -f -
  kubectl -n <ns> rollout status deployment/sdlc-platform --timeout=300s
  ```
- ConfigMap/Secret 改动后需 `kubectl -n <ns> rollout restart deployment/sdlc-platform` 生效。

## 6. 验收门（每次上线必过；任一不满足 = 上线未完成）

1. 三探针过、pod Running：`kubectl -n <ns> get pods -w`；`describe pod` 核 fsGroup=1000 生效、init-data 完成。
2. `curl http://<内网地址>/api/health` 与 `/api/health/ready` 皆 200。
3. 浏览器走通业务流：建卡 → source_import → 质检 → 澄清答复 → 发布 → 设计 → 门禁。
4. 容器内时区：`kubectl exec <pod> -- date` 应为 +08:00（偏 8 小时 = tzdata 层失效，立即排查）。
5. agent 通道：`curl http://<内网地址>/api/agent/status` → `enabled:true, paused:false, running:true`；
   跑一条冒烟任务（或 `node scripts/verify-agent.mjs http://<内网地址>`，需模型余额）。
6. 黑盒全量：`bash scripts/verify-all.sh http://<内网地址>` 全绿。
   ⚠ **与写令牌互斥**：verify 脚本族无 X-Token 通道（唯 verify-token 自建实例有）——若 Secret 已配
   `SDLC_WRITE_TOKEN`，全部写面脚本 401。验收流程：**窗口内先不配 token 跑全量** → 验收过后
   `kubectl -n <ns> create secret … --from-literal=SDLC_WRITE_TOKEN=…`（或 patch）+ rollout restart
   → 用带 `-H "X-Token: <token>"` 的 curl 写面轻量复核（如建一张测试卡后删除）。
7. 韧性：`kubectl -n <ns> delete pod <pod>` 重建后 `/data` 数据无损（建过的需求卡还在）。

## 7. 待运维确认清单（上线前清零）

| # | 项 | 影响 |
|---|---|---|
| 1 | namespace 归属 | groovy/configmap/secret/pvc 的 <ns> |
| 2 | storageClass 名 + **reclaimPolicy 须 Retain** | pvc.yaml；Delete 策略下 PVC 误删即文件数据全失 |
| 3 | 日志采集方式（stdout vs hostPath applogs） | 现按 stdout + logType 标签；定 hostPath 则补卷挂载 |
| 4 | MySQL 建库申请流程 + Pod 直连网络策略 | DSN 可达性；不通则 Service/网络策略开通 |
| 5 | SWR org/region 终值（cn-north-4 vs cn-east-3 两见） | build.sh REGISTRY 与 groovy IMAGE_URL |
| 6 | swr-secret 在目标 namespace 是否已建 | 镜像拉取 |

## 8. 数据边界与备份（决策记录）

- **不自具备份 CronJob**（2026-10-29 拍板）：MySQL 库依赖公司 DB 平台的备份/恢复能力，恢复演练责任在平台侧。
- **PVC `/data` 文件面不在 DB 备份覆盖内**：`media/`（上传媒体）、`tasks/<id>/out/`（source_import 服务端
  直读产物——确定性管道红线）、`sdlc/<reqId>/`（任务工作区）。需恢复时靠 PVC 快照 / 手工 rsync 自助；
  定期（建议每月）`kubectl cp` 或快照留存关键需求的工作区。
- 本地开发库快照工具 `scripts/backup.sh`（mysqldump）保留为开发自助面，与线上无关。

## 9. 升级与回滚

- 升级 = 新镜像 tag → groovy（或手工 sed+apply）→ rollout status。数据库结构由启动期幂等迁移自动补列。
- 回滚 = 换旧 tag 重新渲染 apply（`kubectl rollout undo` 亦可，Recreate 下等效）。**只回滚到 schema 兼容的
  版本**——迁移只加不删，旧代码跑新 schema 安全；反向（新代码跑旧 schema）不保证。
- 紧急停领任务不停服：`curl -X POST http://<内网地址>/api/agent/pause`（resume 恢复）。

## 10. 已知限制

- 发布必停机（Recreate 窗口最坏 2~3 分钟），选业务空闲窗口。
- 发布中断在跑 agent 任务与 SSE 流：前者由 expire_stale + sweeper 300s 置 failed 可重试，后者客户端重连。
- readiness 最坏判负延迟由 `connect_timeout=10s` 决定（DB 整机失联时探针 3s 超时先摘流，方向正确）。
- 媒体上传上限 64MB（`media_max_bytes`）；内网直访 Service 无 ingress 时无 body size 中间层顾虑。
- 单 worker 模型下长阻塞 DB 调用（read_timeout=60s 兜底）会拖慢并发的其他请求——属 RLock 串行既定代价。
