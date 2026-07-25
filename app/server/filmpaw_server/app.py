"""FastAPI application factory."""

import threading
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI

from filmpaw_server import __version__
from filmpaw_server.db import connect
from filmpaw_server.routes_performers import router as performers_router
from filmpaw_server.routes_sources import router as sources_router


def create_app(db_path: Path | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.db = connect(db_path)
        # One shared connection + one lock: FastAPI sync endpoints run on
        # worker threads, and overlapping requests (scan vs scan-all) must
        # not interleave on the connection. Single-user local app —
        # serializing all DB work is correct and simple.
        app.state.db_lock = threading.RLock()
        yield
        app.state.db.close()

    app = FastAPI(title="filmpaw-server", version=__version__, lifespan=lifespan)

    @app.get("/api/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "version": __version__}

    app.include_router(sources_router)
    app.include_router(performers_router)
    return app
