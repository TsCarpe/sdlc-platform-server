"""应用装配（对应 Java start-app）：异常处理器族、X-Token 守卫、静态托管五行为、/api/health。

启动：cd server && uv run uvicorn platform_sdlc.main:app --factory --host 0.0.0.0 --port 8080
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from platform_sdlc.common import ApiException, ConflictException, ErrorResult
from platform_sdlc.config import Settings, resolve_paths
from platform_sdlc.db import Database
from platform_sdlc.migrations import run_migrations
from platform_sdlc.repositories import Repos
from platform_sdlc.routers import register_routers
from platform_sdlc.runtime import AppContext
from platform_sdlc.services import Services

logging.basicConfig(level=logging.INFO)  # 单服务部署：INFO 直出 stdout/journald

log = logging.getLogger(__name__)


def create_app(settings: Settings | None = None, cwd: Path | None = None) -> FastAPI:
    settings = settings or Settings()
    paths = resolve_paths(settings, cwd)
    db = Database(paths.db)
    run_migrations(db)
    repos = Repos.create(db)
    services = Services.create(db, repos, paths.media_dir, settings.media_max_bytes)
    ctx = AppContext(settings=settings, paths=paths, db=db, repos=repos, services=services)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        log.info("启动：db=%s media=%s static=%s", paths.db, paths.media_dir, settings.static_dir)
        if settings.agent_enabled:
            from platform_sdlc.agent.loop import AgentLoop

            ctx.agent = AgentLoop(settings, repos, services)
            ctx.agent.start()  # Phase ②：进程内任务 agent（openai-agents），外置 runner 通道不受影响
        yield
        if ctx.agent is not None:
            await ctx.agent.stop()
        from platform_sdlc.agent.assistant import close_shared_session_db

        close_shared_session_db()  # assistant.db 共享连接显式关闭（AUDIT D5-1）

    app = FastAPI(title="sdlc-platform", lifespan=lifespan, redirect_slashes=False)
    _install_exception_handlers(app)
    _install_middleware(app, settings)
    register_routers(app, ctx)
    _mount_static(app, settings)
    return app


# ---------- 异常处理器族（对应 GlobalExceptionHandler：错误统一 {error}，键形级兼容） ----------


def _install_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(ConflictException)
    async def conflict(_: Request, exc: ConflictException) -> JSONResponse:
        # 乐观锁冲突：409 body 携带 conflicts 明细（键形见 verify-guard 断言）
        return JSONResponse({"error": str(exc), "conflicts": exc.conflicts}, status_code=409)

    @app.exception_handler(ApiException)
    async def api(_: Request, exc: ApiException) -> JSONResponse:
        return JSONResponse(ErrorResult(error=str(exc)).model_dump(), status_code=exc.status)

    @app.exception_handler(RequestValidationError)
    async def validation(_: Request, exc: RequestValidationError) -> JSONResponse:
        # 区分 body 绑定失败（Java HttpMessageNotReadable 固定文案）与 query 参数错误
        if any(tuple(e.get("loc", ()))[0:1] == ("body",) for e in exc.errors()):
            msg = "请求体不是合法 JSON 或字段类型不符"
        else:
            msg = "请求参数错误：" + "; ".join(f"{'.'.join(str(x) for x in e.get('loc', ()))}: {e.get('msg', '')}" for e in exc.errors())
        return JSONResponse(ErrorResult(error=msg).model_dump(), status_code=400)

    @app.exception_handler(StarletteHTTPException)
    async def http_exc(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        if exc.status_code == 405:
            return JSONResponse(ErrorResult(error=f"请求方法不支持：{request.method}").model_dump(), status_code=405)
        if exc.status_code == 404:
            return JSONResponse(ErrorResult(error=f"资源不存在：{request.url.path}").model_dump(), status_code=404)
        return JSONResponse(ErrorResult(error=str(exc.detail)).model_dump(), status_code=exc.status_code)

    @app.exception_handler(Exception)
    async def generic(_: Request, exc: Exception) -> JSONResponse:
        log.exception("服务内部错误")
        return JSONResponse(ErrorResult(error=f"服务内部错误：{type(exc).__name__}").model_dump(), status_code=500)


# ---------- 中间件：X-Token 写守卫 + 静态缓存策略 ----------


def _install_middleware(app: FastAPI, settings: Settings) -> None:
    @app.middleware("http")
    async def token_guard(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        # 对应 TokenFilter：仅拦非 GET 且 /api/ 前缀；write-token 为空不启用（v1 无登录共识）
        is_write = request.method != "GET" and request.url.path.startswith("/api/")
        if is_write and settings.write_token and request.headers.get("X-Token") != settings.write_token:
            # sendError 形状 = Spring Boot 默认错误体（message 默认不输出）
            return JSONResponse(
                {
                    "timestamp": datetime.now().isoformat(timespec="milliseconds"),
                    "status": 401,
                    "error": "Unauthorized",
                    "path": request.url.path,
                },
                status_code=401,
            )
        return await call_next(request)

    @app.middleware("http")
    async def static_cache(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        response = await call_next(request)
        path = request.url.path
        if path.startswith("/assets/"):
            response.headers["Cache-Control"] = "max-age=31536000, public"  # hash 文件名 → 长缓存
        elif path in ("/", "/index.html"):
            response.headers["Cache-Control"] = "no-cache"  # 重建后旧 HTML 引用已删 assets 会白屏
        return response


# ---------- 静态托管五行为（对应 WebStaticConfig） ----------


def _safe_static_file(static_root: Path, path: str) -> Path | None:
    """静态根内且带扩展名的文件（is_relative_to 逐组件比较，防兄弟目录前缀绕过）；否则 None。"""
    if "." not in path:
        return None
    candidate = (static_root / path).resolve()
    if candidate.is_relative_to(static_root.resolve()) and candidate.is_file():
        return candidate
    return None


def _mount_static(app: FastAPI, settings: Settings) -> None:
    static_root = Path(settings.static_dir)
    if not static_root.is_dir():
        log.warning("静态目录不存在（跳过前端托管）：%s", static_root)
        return
    index = static_root / "index.html"

    # /assets 长缓存（hash 文件名）——挂载先于兜底路由注册，路径优先命中
    if (static_root / "assets").is_dir():
        app.mount("/assets", StaticFiles(directory=str(static_root / "assets")), name="assets")

    def spa_index() -> FileResponse:
        return FileResponse(index, headers={"Cache-Control": "no-cache"})

    @app.get("/")
    async def root() -> FileResponse:
        return spa_index()

    @app.get("/index.html")
    async def index_html() -> FileResponse:
        return spa_index()

    @app.get("/favicon.ico", include_in_schema=False)
    async def favicon() -> Response:
        # 浏览器（缓存/历史书签）仍会请求 favicon.ico：转 svg，避免 404 噪音
        return RedirectResponse("/favicon.svg", status_code=302)

    @app.get("/{path:path}", include_in_schema=False, response_model=None)
    async def static_or_spa(request: Request, path: str) -> Response:
        # API 面：区分 405（路径命中但方法不符）与 404（未知 API 路径）——对齐 Spring 语义
        if path == "api" or path.startswith("api/") or path == "assets" or path.startswith("assets/"):
            from starlette.routing import Match

            for route in app.router.routes:
                try:
                    match, _ = route.matches(request.scope)
                except Exception:
                    continue
                if match == Match.PARTIAL:
                    raise StarletteHTTPException(status_code=405)
            raise StarletteHTTPException(status_code=404)
        # 根级静态文件（favicon.svg 等带扩展名）；SPA 回退（无扩展名路径 forward index.html）
        candidate = _safe_static_file(static_root, path)
        if candidate is not None:
            return FileResponse(candidate)
        return spa_index()
