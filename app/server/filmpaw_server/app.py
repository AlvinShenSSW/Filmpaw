"""FastAPI application factory."""

import os
import threading
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from filmpaw_server import __version__
from filmpaw_server.db import connect, default_db_path
from filmpaw_server.routes_performers import router as performers_router
from filmpaw_server.routes_sources import router as sources_router


def create_app(db_path: Path | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.db = connect(db_path)
        app.state.db_path = str(db_path or default_db_path())
        # One shared connection + one lock: FastAPI sync endpoints run on
        # worker threads, and overlapping requests (scan vs scan-all) must
        # not interleave on the connection. Single-user local app —
        # serializing all DB work is correct and simple.
        app.state.db_lock = threading.RLock()
        yield
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
    return app
