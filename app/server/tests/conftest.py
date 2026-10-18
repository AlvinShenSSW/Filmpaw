import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from filmpaw_server.app import create_app
from filmpaw_server.db import connect

SESSION_TOKEN = "a" * 64


@pytest.fixture(autouse=True)
def session_environment(monkeypatch):
    monkeypatch.setenv("FILMPAW_SESSION_TOKEN", SESSION_TOKEN)


@pytest.fixture()
def db(tmp_path: Path) -> sqlite3.Connection:
    conn = connect(tmp_path / "test.db")
    yield conn
    conn.close()


@pytest.fixture()
def client(tmp_path: Path) -> TestClient:
    app = create_app(db_path=tmp_path / "api.db")
    with TestClient(app, headers={"X-Filmpaw-Session": SESSION_TOKEN}) as c:
        yield c


@pytest.fixture()
def source_dir(tmp_path: Path) -> Path:
    root = tmp_path / "nas" / "女优VI"
    root.mkdir(parents=True)
    return root


def add_performer_folder(root: Path, name: str, with_jpg: bool = False, color=(200, 60, 40)) -> Path:
    d = root / name
    d.mkdir(exist_ok=True)
    if with_jpg:
        Image.new("RGB", (640, 960), color).save(d / "folder.jpg", quality=90)
    return d
