from fastapi.testclient import TestClient

from filmpaw_server import __version__
from filmpaw_server.app import create_app


def test_health_ok() -> None:
    client = TestClient(create_app())
    resp = client.get("/api/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["version"] == __version__
    assert "db_path" not in body  # not leaked here; settings carries it


def test_health_unknown_route_404() -> None:
    client = TestClient(create_app())
    assert client.get("/api/nope").status_code == 404


def test_cors_allows_ui_origins() -> None:
    """Codex P1 regression: UI origins (dev + Tauri) must receive CORS
    headers or every sidecar call is browser-blocked."""
    client = TestClient(create_app())
    for origin in ("http://localhost:3000", "http://tauri.localhost"):
        r = client.get("/api/health", headers={"Origin": origin})
        assert r.headers.get("access-control-allow-origin") == origin
    r = client.get("/api/health", headers={"Origin": "http://evil.example"})
    assert "access-control-allow-origin" not in r.headers
