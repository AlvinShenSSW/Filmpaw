"""Session authorization and archive HTTP contracts, using disposable data only."""

import time

import pytest
from fastapi.testclient import TestClient

from filmpaw_server.app import create_app

TOKEN = "a" * 64


@pytest.mark.parametrize("development", ["0", "1"])
def test_mutations_require_session_even_in_development(tmp_path, monkeypatch, development):
    monkeypatch.setenv("FILMPAW_SESSION_TOKEN", TOKEN)
    monkeypatch.setenv("FILMPAW_DEV", development)
    with TestClient(create_app(tmp_path / "auth.db")) as client:
        for headers in ({}, {"X-Filmpaw-Session": "wrong"}):
            response = client.put("/api/settings", json={"last_local_dir": "anything"}, headers=headers)
            assert response.status_code == 403
        assert client.get("/api/health").status_code == 200
        assert client.get("/api/archive/transfers/latest").status_code == 403
        assert client.get("/api/archive/transfers/latest", headers={"X-Filmpaw-Session": TOKEN}).json() is None


def test_session_does_not_accept_foreign_origin(tmp_path, monkeypatch):
    monkeypatch.setenv("FILMPAW_SESSION_TOKEN", TOKEN)
    with TestClient(create_app(tmp_path / "origin.db")) as client:
        result = client.post("/api/archive/prepare-exit", headers={
            "X-Filmpaw-Session": TOKEN, "Origin": "https://unrelated.example",
        })
        assert result.status_code == 403


def test_unconfigured_or_invalid_token_is_fail_closed(tmp_path, monkeypatch):
    monkeypatch.delenv("FILMPAW_SESSION_TOKEN", raising=False)
    with TestClient(create_app(tmp_path / "closed.db")) as client:
        result = client.post("/api/archive/prepare-exit", headers={"X-Filmpaw-Session": TOKEN})
        assert result.status_code == 403


def test_only_one_service_can_recover_and_move_for_a_library(tmp_path):
    path = tmp_path / "shared.db"
    with TestClient(create_app(path)) as first:
        with pytest.raises(RuntimeError, match="另一个"):
            with TestClient(create_app(path)):
                pass
        assert first.get("/api/health").status_code == 200
    # OS lock is released on normal exit; a stale lock filename is harmless.
    with TestClient(create_app(path)) as restarted:
        assert restarted.get("/api/health").status_code == 200


def seed_pair(client, tmp_path):
    batch = tmp_path / "batch"
    source = batch / "Actor"
    nas = tmp_path / "nas"
    target = nas / "Actor"
    source.mkdir(parents=True)
    target.mkdir(parents=True)
    movie = source / "Film, 10"
    movie.mkdir()
    (movie / "video.mp4").write_bytes(b"fixture-video")
    (movie / "movie.nfo").write_text("fixture metadata")
    sid = client.post("/api/sources", json={"unc_path": str(nas)}).json()["id"]
    assert client.post(f"/api/sources/{sid}/scan").status_code == 200
    pid = client.get("/api/performers").json()["items"][0]["id"]
    assert client.put("/api/settings", json={"last_local_dir": str(batch)}).status_code == 200
    return batch, source, target, pid


def test_archive_roundtrip_and_exit_admission(client, tmp_path):
    batch, source, target, pid = seed_pair(client, tmp_path)
    result = client.post("/api/archive/contexts", json={
        "local_dir": str(batch), "subdir": "Actor", "performer_id": pid,
    })
    assert result.status_code == 201, result.text
    context = result.json()
    assert [item["name"] for item in context["source_items"]] == ["Film, 10"]
    payload = {"context_id": context["id"], "names": ["Film, 10"], "request_id": "fixture-1"}
    result = client.post("/api/archive/transfers", json=payload)
    assert result.status_code == 202, result.text
    task_id = result.json()["id"]
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        task = client.get(f"/api/archive/transfers/{task_id}").json()
        if task["status"] not in ("queued", "running"):
            break
        time.sleep(.02)
    assert task["status"] == "completed", task
    assert not (source / "Film, 10").exists()
    assert (target / "Film, 10" / "video.mp4").read_bytes() == b"fixture-video"
    assert client.post("/api/archive/transfers", json=payload).json()["id"] == task_id
    assert client.get("/api/archive/transfers/latest").json()["id"] == task_id
    assert "manifest" not in str(task)
    assert client.post("/api/archive/prepare-exit").json() == {"safe": True, "task_id": None}


def test_arbitrary_paths_and_reverse_parameters_are_not_accepted(client, tmp_path):
    batch, _, _, pid = seed_pair(client, tmp_path)
    context_body = {"local_dir": str(batch), "subdir": "Actor", "performer_id": pid}
    assert client.post("/api/archive/contexts", json={**context_body, "target_path": "C:/"}).status_code == 422
    assert client.post("/api/archive/transfers", json={
        "context_id": "any", "names": ["Film"], "request_id": "request", "reverse": True,
    }).status_code == 422


def test_switching_batch_away_and_back_invalidates_old_context(client, tmp_path):
    batch, source, target, pid = seed_pair(client, tmp_path)
    context = client.post("/api/archive/contexts", json={
        "local_dir": str(batch), "subdir": "Actor", "performer_id": pid,
    }).json()
    other = tmp_path / "other-batch"
    other.mkdir()
    client.put("/api/settings", json={"last_local_dir": str(other)})
    client.put("/api/settings", json={"last_local_dir": str(batch)})
    result = client.post("/api/archive/transfers", json={
        "context_id": context["id"], "names": ["Film, 10"], "request_id": "old-context",
    })
    assert result.status_code == 409
    assert (source / "Film, 10").is_dir()
    assert not (target / "Film, 10").exists()


def test_v2_migration_keeps_existing_library_and_adds_journal(tmp_path):
    import sqlite3
    from filmpaw_server.db import connect
    from tests.test_migration import V1_DDL

    path = tmp_path / "v2.db"
    conn = sqlite3.connect(path)
    conn.executescript(V1_DDL)
    conn.execute("ALTER TABLE performers ADD COLUMN thumb_side INTEGER")
    conn.execute("INSERT INTO schema_version VALUES (2)")
    conn.execute("INSERT INTO settings VALUES ('last_local_dir', 'untouched')")
    conn.execute("INSERT INTO sources(id, unc_path, label) VALUES (7, 'nas-root', 'keep')")
    conn.execute("INSERT INTO aliases(name_norm, alias, alias_norm) VALUES ('actor', 'alias', 'alias')")
    conn.commit()
    conn.close()
    conn = connect(path)
    assert conn.execute("SELECT version FROM schema_version").fetchone()[0] == 3
    assert conn.execute("SELECT value FROM settings").fetchone()[0] == "untouched"
    assert conn.execute("SELECT label FROM sources").fetchone()[0] == "keep"
    assert conn.execute("SELECT alias FROM aliases").fetchone()[0] == "alias"
    assert conn.execute("SELECT count(*) FROM archive_tasks").fetchone()[0] == 0
    conn.close()


def test_v3_journal_migration_rolls_back_ddl_and_version(tmp_path, monkeypatch):
    import sqlite3
    import filmpaw_server.db as db_module
    from tests.test_migration import V1_DDL

    path = tmp_path / "interrupted-v2.db"
    with sqlite3.connect(path) as conn:
        conn.executescript(V1_DDL)
        conn.execute("ALTER TABLE performers ADD COLUMN thumb_side INTEGER")
        conn.execute("INSERT INTO schema_version VALUES (2)")
    migrations = dict(db_module._MIGRATIONS)

    def crash(conn):
        migrations[2](conn)
        raise RuntimeError("interrupted v3 migration")

    monkeypatch.setattr(db_module, "_MIGRATIONS", {**migrations, 2: crash})
    with pytest.raises(RuntimeError, match="interrupted v3"):
        db_module.connect(path)
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT version FROM schema_version").fetchone()[0] == 2
        assert not conn.execute("SELECT name FROM sqlite_master WHERE name='archive_tasks'").fetchall()
    monkeypatch.setattr(db_module, "_MIGRATIONS", migrations)
    db_module.connect(path).close()
