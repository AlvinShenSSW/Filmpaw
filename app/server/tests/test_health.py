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


def test_health_unknown_route_404() -> None:
    client = TestClient(create_app())
    assert client.get("/api/nope").status_code == 404
