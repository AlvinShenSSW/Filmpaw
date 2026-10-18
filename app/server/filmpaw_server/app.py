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
from filmpaw_server.db import connect, default_db_path
from filmpaw_server.routes_archive import router as archive_router
from filmpaw_server.routes_performers import router as performers_router
from filmpaw_server.routes_sources import router as sources_router
from filmpaw_server.session import library_session
from filmpaw_server.transfer import TransferError, TransferService


def create_app(db_path: Path | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        with library_session(db_path or default_db_path()):
            app.state.db = connect(db_path)
            app.state.db_path = str((db_path or default_db_path()).resolve())
            # The process lock protects journal recovery across sidecars.
            # This short-lived thread lock protects the shared SQLite connection.
            app.state.db_lock = threading.RLock()
            app.state.transfers = TransferService(app.state.db, app.state.db_lock)
            try:
                app.state.transfers.recover()
                yield
            finally:
                app.state.transfers.close()
                app.state.db.close()

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

    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_methods=["*"],
        allow_headers=["*"],
    )

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
    return app
