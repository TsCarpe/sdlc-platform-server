#!/bin/bash
# 构建推送 SDLC 平台后端镜像（EXEC-PLAN-K8s 阶段 1；CI 已拍板暂跳过——本脚本是镜像唯一产出口）。
# 2026-10-30 目标收窄：K8s 编排归运维，交付物 = 自带配置的镜像（env 构建期 baked 进 /opt/app/.env）；
# 前端出镜像（dist 由部署侧静态托管），本脚本只产纯后端 API 镜像。
# 四件事：
#   ① 暂存仓外依赖：skills 仓（剔除 .git/poc 死重）rsync 进 .build/ 构建上下文
#   ② 固化 skills 版本（REVISION 进镜像 + parse_doc --media-manifest 构建期门禁）
#   ③ 暂存配置（deploy/env.image → .build/env，DSN 空 = 构建期红——打出来也起不来）
#   ④ docker build（基础镜像走 SWR docker.io 代理，前置 pull 探测早失败；PUSH=1 时推 SWR）
# 用法：deploy/build.sh                                    # 只构建（附带 local 便利 tag）
#       ENV_FILE=deploy/env.verify deploy/build.sh         # 验证栈专用配置（compose/server.verify.yml 配套）
#       REGISTRY=… ORG=… PUSH=1 deploy/build.sh            # 构建并推送（需先 docker login SWR）
# 参数（env）：REGISTRY（默认 cn-north-4 代理域，应用镜像 region 待运维确认——hrsystem 样例
#             另见 cn-east-3）/ ORG（默认 hlyz）/ TAG（默认 日期-短rev）/
#             ENV_FILE（默认 deploy/env.image，拷 deploy/env.image.example 填真值；gitignore 不入库）/
#             SKILLS_REPO（默认 ~/IdeaProjects/claude-sdlc-skills）
set -euo pipefail
cd "$(dirname "$0")/.."

REGISTRY="${REGISTRY:-swr.cn-north-4.myhuaweicloud.com}"
ORG="${ORG:-hlyz}"
TAG="${TAG:-$(date +%Y%m%d%H%M)-$(git rev-parse --short HEAD)}"
ENV_FILE="${ENV_FILE:-deploy/env.image}"
SKILLS_REPO="${SKILLS_REPO:-$HOME/IdeaProjects/claude-sdlc-skills}"
IMAGE="$REGISTRY/$ORG/sdlc-platform-server:$TAG"

echo "== 前置探测 =="
for cmd in docker rsync git; do
  command -v "$cmd" >/dev/null 2>&1 || { echo "缺 $cmd 命令" >&2; exit 1; }
done
# SWR 代理可用性早失败：拉不到基础镜像构建必然红，早暴露好过 build 到一半才炸
PY_BASE="swr.cn-north-4.myhuaweicloud.com/ddn-k8s/docker.io/library/python:3.12-slim"
docker pull -q "$PY_BASE" >/dev/null && echo "python 基础镜像代理 OK"

mkdir -p .build/skills   # macOS 自带 rsync 2.6.9 不自动建目标目录

echo "== 暂存 skills 仓：$SKILLS_REPO -> .build/skills/ =="
[ -d "$SKILLS_REPO/skills" ] || { echo "skills 仓不存在：$SKILLS_REPO（可用 SKILLS_REPO 覆盖）" >&2; exit 1; }
# 整体进镜像（_fuzzy_find 会 rglob 全仓，不能只拷被引用子集）；剔除 .git(33M)/poc(31M) 死重后约 2M
rsync -a --delete --exclude .git --exclude poc --exclude __pycache__ --exclude '*.bak*' "$SKILLS_REPO/" .build/skills/
# 版本固化：commit + 主题写 REVISION 进镜像（上线后排障溯源；镜像内 cat /opt/app/skills/REVISION）
{
  git -C "$SKILLS_REPO" rev-parse HEAD
  git -C "$SKILLS_REPO" log -1 --format='%h %ad %s' --date=short
} > .build/skills/REVISION
cat .build/skills/REVISION

echo "== skills 版本门禁（parse_doc --media-manifest，≥2026-10-28）=="
python3 "$SKILLS_REPO/skills/sdlc-intent/scripts/parse_doc.py" --help 2>/dev/null | grep -q -- --media-manifest \
  || { echo "skills 仓版本不满足（缺 --media-manifest）——先更新 skills 仓再构建" >&2; exit 1; }
echo "门禁通过"

echo "== 暂存配置：$ENV_FILE -> .build/env =="
[ -f "$ENV_FILE" ] || { echo "配置文件不存在：${ENV_FILE}（拷 deploy/env.image.example 为 deploy/env.image 填真值，或 ENV_FILE= 覆盖路径）" >&2; exit 1; }
cp "$ENV_FILE" .build/env
# 配置门禁：DSN 必填非空（空串启动即 crash-loop，与其上线炸不如构建期红）
grep -Eq '^SDLC_DB_DSN=mysql://.+' .build/env \
  || { echo "SDLC_DB_DSN 未配置或为空（${ENV_FILE}）——镜像打出来也起不来，构建期拦截" >&2; exit 1; }
grep -q '^SDLC_AGENT_ENABLED=true' .build/env \
  || echo "⚠ SDLC_AGENT_ENABLED 未开启：任务不会自动执行（不阻断）"

echo "== docker build：$IMAGE =="
docker build -f Dockerfile -t "$IMAGE" -t sdlc-platform-server:local .

if [ "${PUSH:-0}" = "1" ]; then
  echo "== push SWR（需先 docker login）=="
  docker push "$IMAGE"
fi

echo
echo "IMAGE_URL=$REGISTRY/$ORG/sdlc-platform-server"
echo "IMAGE_TAG=$TAG"
echo "（PUSH=1 时上两行即 SWR 镜像地址；本地容器验证用 sdlc-platform-server:local）"
