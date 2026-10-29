"""配置与数据目录定位（对应 Java application.yml + DataDirEnvironmentPostProcessor）。

env 前缀 SDLC_：SDLC_PORT / SDLC_DB_PATH / SDLC_WRITE_TOKEN / SDLC_AGENT_ENABLED …
"""

from __future__ import annotations

from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="SDLC_", env_file=".env", extra="ignore")

    host: str = "0.0.0.0"  # 与 Java 现状一致（application.yml address: 0.0.0.0，局域网可访问）
    port: int = 8888

    write_token: str = ""  # 为空不启用；设置后非 GET 的 /api 写接口要求 X-Token 头
    # MySQL DSN（EXEC-PLAN-K8s 阶段 2）：空串 = 启动即报错带指引（漏配要 crash-loop 可见，不给静默默认）
    db_dsn: str = ""
    media_dir: str = "./data/media"  # 相对路径基于 cwd 直解（./data/media → <cwd>/data/media）
    media_max_bytes: int = 64 * 1024 * 1024  # 媒体上传上限：路由层 Content-Length 预检 + 服务层兜底
    static_dir: str = "static"  # vite 构建产物目录（约定 uvicorn 以 server/ 为工作目录启动）

    # v2.1 进程内 agent（openai-agents）；P0–P3 阶段保持 False，P4 起由部署环境打开
    agent_enabled: bool = False
    agent_poll_interval_ms: int = 3000
    agent_timeout_min: int = 30
    gate_timeout_min: int = 60  # A/B 级评审专项：主会话+4 子 run+汇合共享同一墙钟，30min 不够
    agent_max_iters: int = 160
    agent_base_url: str = ""
    agent_model: str = ""
    agent_api_key: str = ""
    agent_skills_repo: str = "~/IdeaProjects/claude-sdlc-skills"
    agent_workspace_root: str = "."

    # 飞书接入（DESIGN-飞书接入Python化 §6/§8）：凭据为空 = 无飞书能力（prompt 不生成
    # 飞书分支、工具不注册、lark-cli 命令白名单剔除——三处联动防模型 fallback）
    feishu_app_id: str = ""
    feishu_app_secret: str = ""
    feishu_media_max_bytes: int = 10 * 1024 * 1024  # 大附件阈值：超限登记元数据 skipped:size-exceeded


@dataclass(frozen=True)
class ResolvedPaths:
    """启动期一次解析完成的绝对路径；全程序只读这一份（MySQL 化后无 db 文件，
    assistant 会话亦并入主库——数据目录锚点仅剩 media_dir）。"""

    media_dir: Path
    workspace_root: Path
    repo_root: Path


def _ensure_parent(p: Path) -> Path:
    with suppress(OSError):
        p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _is_external(p: str) -> bool:
    return p.startswith("/") or p.startswith("~")


def resolve_paths(settings: Settings, cwd: Path | None = None) -> ResolvedPaths:
    """路径定位（MySQL 化后无 db 文件，media_dir 相对路径基于 cwd 直解）。

    规则（media_dir 为相对路径时）：./data/media → <cwd>/data/media（uvicorn 以仓根为 cwd）；
    K8s / 测试传绝对路径直接采用。data_dir = media_dir.parent（数据目录锚点：
    assistant.db 等目录级文件挂在它下面）。
    """
    cwd = (cwd or Path.cwd()).resolve()
    repo_root = cwd.parent if cwd.name == "server" else (cwd if (cwd / "server").is_dir() else cwd)

    media_raw = settings.media_dir
    media_dir = Path(media_raw).expanduser() if _is_external(media_raw) else _ensure_parent(cwd / media_raw.removeprefix("./"))

    ws_raw = settings.agent_workspace_root
    if _is_external(ws_raw):
        workspace_root = Path(ws_raw).expanduser()
    else:
        rel = "" if ws_raw in ("..", "./", ".") else ws_raw
        workspace_root = repo_root / rel

    return ResolvedPaths(media_dir=media_dir, workspace_root=workspace_root, repo_root=repo_root)
