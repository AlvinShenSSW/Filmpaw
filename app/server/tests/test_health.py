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


def test_cors_allows_ui_origins(monkeypatch) -> None:
    """Codex P1 regression: UI origins (dev + Tauri) must receive CORS
    headers or every sidecar call is browser-blocked."""
    monkeypatch.setenv("FILMPAW_DEV", "1")
    client = TestClient(create_app())
    for origin in ("http://localhost:3000", "http://tauri.localhost"):
        r = client.get("/api/health", headers={"Origin": origin})
        assert r.headers.get("access-control-allow-origin") == origin
    r = client.get("/api/health", headers={"Origin": "http://evil.example"})
    assert "access-control-allow-origin" not in r.headers


def test_cors_preflight_for_mutations(monkeypatch) -> None:
    """Kimi minor: mutations rely on OPTIONS preflight, not just simple GET."""
    monkeypatch.setenv("FILMPAW_DEV", "1")
    client = TestClient(create_app())
    r = client.options(
        "/api/sources",
        headers={
            "Origin": "http://localhost:3000",
            "Access-Control-Request-Method": "POST",
        },
    )
    assert r.status_code == 200
    assert "POST" in r.headers.get("access-control-allow-methods", "")


def test_cors_dev_origins_off_by_default(monkeypatch) -> None:
    """Kimi P2: a shipped app must not trust dev-server origins."""
    monkeypatch.delenv("FILMPAW_DEV", raising=False)
    client = TestClient(create_app())
    r = client.get("/api/health", headers={"Origin": "http://localhost:3000"})
    assert "access-control-allow-origin" not in r.headers
    r2 = client.get("/api/health", headers={"Origin": "http://tauri.localhost"})
    assert r2.headers.get("access-control-allow-origin") == "http://tauri.localhost"
