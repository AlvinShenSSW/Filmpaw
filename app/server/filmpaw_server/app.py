"""FastAPI application factory."""

from fastapi import FastAPI

from filmpaw_server import __version__


def create_app() -> FastAPI:
    app = FastAPI(title="filmpaw-server", version=__version__)

    @app.get("/api/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "version": __version__}

    return app
