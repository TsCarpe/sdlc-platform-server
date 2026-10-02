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
    port: int = 8080

    write_token: str = ""  # 为空不启用；设置后非 GET 的 /api 写接口要求 X-Token 头
    db_path: str = "./data/platform.db"  # 相对路径走 resolve_paths 的数据目录定位规则
    media_dir: str = "./data/media"
    static_dir: str = "static"  # vite 构建产物目录（约定 uvicorn 以 server/ 为工作目录启动）

    # v2.1 进程内 agent（openai-agents）；P0–P3 阶段保持 False，P4 起由部署环境打开
    agent_enabled: bool = False
    agent_poll_interval_ms: int = 3000
    agent_timeout_min: int = 30
    agent_max_iters: int = 160
    agent_base_url: str = ""
    agent_model: str = ""
    agent_api_key: str = ""
    agent_skills_repo: str = "~/IdeaProjects/claude-sdlc-skills"
    agent_workspace_root: str = "."


@dataclass(frozen=True)
class ResolvedPaths:
    """启动期一次解析完成的绝对路径；全程序只读这一份。"""

    db: Path
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
    """数据目录自适应定位（移植 DataDirEnvironmentPostProcessor，backend 语义换成 server）。

    规则（db_path 为相对路径时）：
      1. 工作目录下已存在该数据目录 → 就用它（server/ 启动、独立部署目录）
      2. 工作目录下有 server/ 子目录（仓库根启动）→ 落到 server/data
      3. 都没有 → 在工作目录创建 data/（全新环境）
    """
    cwd = (cwd or Path.cwd()).resolve()
    repo_root = cwd.parent if cwd.name == "server" else (cwd if (cwd / "server").is_dir() else cwd)

    db_raw = settings.db_path
    if _is_external(db_raw):
        db = Path(db_raw).expanduser()
    else:
        p = Path(db_raw.removeprefix("./"))
        db_file = p.name or "platform.db"
        parent_name = p.parent.name if p.parent != Path(".") else "data"
        if p.parent != Path(".") and (cwd / p.parent).is_dir():
            db = cwd / p.parent / db_file  # 规则 1
        elif (cwd / "server").is_dir() and not (cwd / parent_name).is_dir():
            db = cwd / "server" / parent_name / db_file  # 规则 2
        else:
            db = cwd / parent_name / db_file  # 规则 3
        db = _ensure_parent(db)

    media_raw = settings.media_dir
    if _is_external(media_raw):
        media_dir = Path(media_raw).expanduser()
    else:
        media_dir = _ensure_parent(db.parent / media_raw.removeprefix("./").removeprefix("data/"))

    ws_raw = settings.agent_workspace_root
    if _is_external(ws_raw):
        workspace_root = Path(ws_raw).expanduser()
    else:
        rel = "" if ws_raw in ("..", "./", ".") else ws_raw
        workspace_root = repo_root / rel

    return ResolvedPaths(db=db, media_dir=media_dir, workspace_root=workspace_root, repo_root=repo_root)
