# SDLC 平台后端镜像（纯 API，单仓自包含）。
# 2026-10-30 前端出镜像：web dist 由部署侧静态托管（与 /api 同源反代，平台无 CORS 中间件）；
# static/ 不存在即纯 API 模式——main._mount_static 既有分支，零代码改动。
# skills 仓已 vendor 进本仓 skills/（原 claude-sdlc-skills 独立仓定格归档）——构建只依赖本仓 clone；
# 配置由 deploy/build.sh 暂存 .build/env（gitignore，含密钥）。
# 基础镜像走 SWR 的 docker.io 代理（公司内网拉取通路）。

# ---------- 运行时 ----------
FROM swr.cn-north-4.myhuaweicloud.com/ddn-k8s/docker.io/library/python:3.12-slim

# TZ 生效两层：tzdata（slim 无 zoneinfo 时 TZ 名解析失败回落 UTC——Times.now() naive 本地时间，
# 全库时间戳会偏 8h）+ ENV。curl 必装：run_command 白名单 + 增量任务「curl 直落 raw/」硬依赖
# （python slim 不带 curl，漏装则上线后第一个变更单任务即失败）；jq 顺带让 curl|jq 管道一次成功。
# 不做 debian 源替换（trixie/bookworm 源路径假设易炸，三个包直装开销可忽略）。
ENV PYTHONUNBUFFERED=1 TZ=Asia/Shanghai UV_LINK_MODE=copy UV_DEFAULT_INDEX=https://mirrors.aliyun.com/pypi/simple/
RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata curl jq \
    && rm -rf /var/lib/apt/lists/* \
    && ln -sf /usr/share/zoneinfo/Asia/Shanghai /etc/localtime \
    && echo "Asia/Shanghai" > /etc/timezone \
    && useradd -u 1000 -m appuser
WORKDIR /opt/app

# uv 与本地生成 uv.lock 的版本对齐（版本漂移读不了 lock 时 --frozen 构建期即失败）
RUN pip install --no-cache-dir -i https://mirrors.aliyun.com/pypi/simple/ "uv==0.11.16"

# 依赖层：先锁文件只装依赖（保层缓存 + --compile-bytecode 启动免重编译）；
# 项目是 editable 安装、无 src 时全量 sync 必失败，故拆两步（src 路径必须与 WORKDIR 一致）
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project --compile-bytecode
COPY src/ ./src/
RUN uv sync --frozen --no-dev

# skills（仓内 vendor 副本；版本随本仓 commit 原子钉死，镜像 tag 即完整版本）
# + 配置 baked（build.sh 暂存 .build/env → /opt/app/.env，pydantic-settings cwd 直读；密钥进镜像层
#   为内网开发环境既定取舍——镜像勿推公网仓库；K8s 编排归运维）
COPY skills/ /opt/app/skills/
COPY .build/env /opt/app/.env

# 镜像级默认（路径类固化；环境变量优先于 env 文件，/opt/app/.env 里配同名路径键不生效。
# 绝对路径直解且程序不建目录——/data 挂卷后目录须存在，卷内目录由运维侧负责）
ENV SDLC_AGENT_SKILLS_REPO=/opt/app/skills SDLC_MEDIA_DIR=/data/media SDLC_AGENT_WORKSPACE_ROOT=/data/ws
RUN mkdir -p /data/media /data/ws && chmod 600 /opt/app/.env && chown -R appuser:appuser /opt/app /data

USER appuser
EXPOSE 8888
# 单进程硬编码 8888（SDLC_PORT 是死配置不消费）。--timeout-graceful-shutdown 30：SSE 聊天流
# 生命周期上限 30min，不设则停机拖满 terminationGracePeriod 120s 被 SIGKILL——30s 主动收尾，
# 在跑任务由启动期 expire_stale + _stale_sweeper 300s 兜底回收。
CMD ["/opt/app/.venv/bin/uvicorn", "platform_sdlc.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8888", "--timeout-graceful-shutdown", "30"]
