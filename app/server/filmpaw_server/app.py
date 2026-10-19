"""FastAPI application factory."""

import hmac
import os
import re
import threading
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from filmpaw_server import __version__
from filmpaw_server.cleanup import CleanupService
from filmpaw_server.db import connect, default_db_path
from filmpaw_server.routes_archive import router as archive_router
from filmpaw_server.routes_cleanup import router as cleanup_router
from filmpaw_server.routes_performers import router as performers_router
from filmpaw_server.routes_sources import router as sources_router
from filmpaw_server.routes_workflow_index import (
    ROUTES as WORKFLOW_ROUTES, WorkflowReadGuard, error_response,
    install_openapi, router as workflow_router,
)
from filmpaw_server.scan_process import ScanSupervisor
from filmpaw_server.session import library_session
from filmpaw_server.transfer import TransferError, TransferService
from filmpaw_server.workflow_index import WorkflowIndex
from filmpaw_server.workflow_models import ScanProfile, WorkflowIndexConfig, WorkflowIndexError


def create_app(db_path: Path | None = None, *, workflow_index: WorkflowIndexConfig | None = None,
               scan_profile: ScanProfile | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # Keep the actual session object reachable if a scanner cannot prove
        # shutdown. An exception must not release the library lease while its
        # live process still owns unresolved scanner handles or I/O.
        session = library_session(db_path or default_db_path())
        session.__enter__()
        app.state.library_session = session
        app.state.shutdown_pending = False
        db = transfers = cleanups = scans = None
        try:
            app.state.db = connect(db_path)
            db = app.state.db
            app.state.db_path = str((db_path or default_db_path()).resolve())
            # The process lock protects journal recovery across sidecars.
            # This short-lived thread lock protects the shared SQLite connection.
            app.state.db_lock = threading.RLock()
            app.state.workflow_index = WorkflowIndex(db, app.state.db_lock, profile=scan_profile)
            app.state.workflow_config = workflow_index
            if workflow_index is not None and workflow_index.library_id != app.state.workflow_index.library_id:
                raise WorkflowIndexError("library_mismatch")
            app.state.transfers = TransferService(app.state.db, app.state.db_lock)
            transfers = app.state.transfers
            app.state.cleanups = CleanupService(app.state.transfers)
            cleanups = app.state.cleanups
            app.state.scans = ScanSupervisor(app.state.workflow_index)
            scans = app.state.scans
            transfers.recover()
            scans.start(auto_refresh=workflow_index is not None)
            yield
        finally:
            if scans is not None:
                from starlette.concurrency import run_in_threadpool
                outcome = await run_in_threadpool(scans.close)
                if not outcome.clean:
                    app.state.shutdown_pending = True
                    raise RuntimeError("扫描退出尚未确认，数据库会话保持占用")
            if cleanups is not None:
                cleanups.close()
            if transfers is not None:
                transfers.close()
            if db is not None:
                db.close()
            session.__exit__(None, None, None)
            app.state.library_session = None

    app = FastAPI(title="filmpaw-server", version=__version__, lifespan=lifespan)
    # The UI runs on a different origin than the sidecar (rsbuild dev
    # localhost:3000; packaged Tauri http://tauri.localhost) — without CORS
    # every browser fetch to 127.0.0.1:<port> is blocked. Dev-server origins
    # are trusted ONLY when FILMPAW_DEV=1 (set by the shell in debug builds):
    # a shipped app must not trust arbitrary pages on localhost:3000.
    origins = ["http://tauri.localhost", "tauri://localhost"]
    if os.environ.get("FILMPAW_DEV") == "1":
        origins += ["http://localhost:3000", "http://127.0.0.1:3000"]
    token = os.environ.get("FILMPAW_SESSION_TOKEN", "")
    configured = bool(re.fullmatch(r"[a-fA-F0-9]{64}", token))

    @app.middleware("http")
    async def session_guard(request: Request, call_next):
        protected = request.method in {"POST", "PUT", "PATCH", "DELETE"} or (
            request.url.path.startswith("/api/archive") and request.method != "OPTIONS"
        )
        if (WORKFLOW_ROUTES.get(request.url.path) == request.method
                and getattr(request.state, "workflow_read_authorized", False)):
            protected = False
        if protected:
            supplied = request.headers.get("X-Filmpaw-Session", "")
            origin = request.headers.get("origin")
            if (
                not configured
                or not hmac.compare_digest(supplied.encode(), token.encode())
                or (origin is not None and origin not in origins)
            ):
                return JSONResponse(status_code=403, content={"detail": "会话凭据无效，请重新启动应用；浏览器开发需配置共享会话凭据"})
        return await call_next(request)

    @app.exception_handler(TransferError)
    async def archive_error(_request: Request, exc: TransferError):
        return JSONResponse(status_code=exc.status_code, content={"detail": str(exc)})

    @app.exception_handler(WorkflowIndexError)
    async def workflow_error(request: Request, exc: WorkflowIndexError):
        if request.url.path.startswith("/api/workflow/"):
            return error_response(request.scope, exc.code, exc.status)
        messages = {
            "scan_exit_pending": "扫描尚未安全结束，已保留窗口，请稍后重试",
            "permission_denied": "无访问权限 — 已保留原有索引",
            "offline": "源暂时不可达 — 已保留原有索引",
            "unknown_owner": "扫描退出待确认，请稍后重试查询",
            "timeout": "扫描超时，尚未取得完整结果",
            "partial": "扫描未完成 — 已保留原有索引",
            "identity_changed": "目录身份已变化，请核对来源",
            "identity_unknown": "无法确认目录身份，请核对来源",
            "not_directory": "路径不存在或不是目录",
            "busy": "已有扫描尚未完成，请稍后重试查询",
            "queue_full": "等待扫描的来源较多，请稍后重试",
            "closing": "正在退出，暂不接受新的扫描",
        }
        return JSONResponse(status_code=exc.status, content={
            "code": exc.code, "detail": messages.get(exc.code, "未能完成扫描，请稍后重试查询"),
        })

    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.add_middleware(WorkflowReadGuard, config=workflow_index, origins=origins)

    class HealthOut(BaseModel):
        status: str
        version: str

    @app.get("/api/health", response_model=HealthOut)
    def health() -> dict[str, str]:
        # NOTE: db_path deliberately NOT exposed here (Kimi review) — the
        # settings endpoint carries it for the UI footer.
        return {"status": "ok", "version": __version__}

    app.include_router(sources_router)
    app.include_router(performers_router)
    app.include_router(archive_router)
    app.include_router(cleanup_router)
    app.include_router(workflow_router)
    install_openapi(app)
    return app
