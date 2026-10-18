"""Cleanup HTTP contracts use disposable batch trees and isolated databases."""

import time

from fastapi.testclient import TestClient

from filmpaw_server.app import create_app


def choose_batch(client, batch):
    batch.mkdir(exist_ok=True)
    response = client.put("/api/settings", json={"last_local_dir": str(batch)})
    assert response.status_code == 200, response.text
    return response.json()["last_local_dir"]


def context_for(client, batch):
    response = client.post("/api/archive/cleanup-contexts", json={"batch_path": str(batch)})
    assert response.status_code == 201, response.text
    return response.json()


def finish(client, task_id):
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        response = client.get(f"/api/archive/cleanups/{task_id}")
        assert response.status_code == 200, response.text
        task = response.json()
        if task["status"] not in {"queued", "running"}:
            return task
        time.sleep(0.01)
    raise AssertionError(f"cleanup did not finish: {task}")


def test_cleanup_only_removes_empty_actors_in_selected_batch(client, tmp_path):
    batch = tmp_path / "batch"
    approved = choose_batch(client, batch)
    (batch / "Empty Actor").mkdir()
    (batch / "Actor With Poster").mkdir()
    (batch / "Actor With Poster" / "folder.jpg").write_bytes(b"actor poster")
    nas = tmp_path / "nas" / "Empty Actor"
    nas.mkdir(parents=True)
    context = context_for(client, approved)
    assert context["batch_path"] == approved
    assert context["batch_revision"]
    response = client.post("/api/archive/cleanups", json={
        "context_id": context["id"], "request_id": "cleanup-api-roundtrip",
    })
    assert response.status_code == 202, response.text
    task = finish(client, response.json()["id"])
    assert task["status"] == "completed", task
    assert task["deleted_count"] == 1
    assert task["kept_count"] == 1
    assert task["failed_count"] == 0
    assert task["batch_path"] == approved
    assert not (batch / "Empty Actor").exists()
    assert (batch / "Actor With Poster" / "folder.jpg").read_bytes() == b"actor poster"
    assert batch.is_dir() and nas.is_dir()
    assert client.get("/api/archive/cleanups/latest").json() == task


def test_cleanup_lost_response_replay_never_deletes_recreated_actor(client, tmp_path):
    batch = tmp_path / "batch"
    approved = choose_batch(client, batch)
    actor = batch / "Empty"
    actor.mkdir()
    context = context_for(client, approved)
    payload = {"context_id": context["id"], "request_id": "same-cleanup"}
    first = client.post("/api/archive/cleanups", json=payload)
    assert first.status_code == 202, first.text
    task = finish(client, first.json()["id"])
    assert task["deleted_count"] == 1
    actor.mkdir()
    replay = client.post("/api/archive/cleanups", json=payload)
    assert replay.status_code == 202, replay.text
    assert replay.json()["id"] == task["id"]
    assert actor.is_dir()
    # A consumed capability cannot authorize a second pass with a new ID.
    again = client.post("/api/archive/cleanups", json={**payload, "request_id": "new-id"})
    assert again.status_code == 409
    assert actor.is_dir()


def test_cleanup_rejects_batch_switch_away_and_back(client, tmp_path):
    batch = tmp_path / "batch"
    approved = choose_batch(client, batch)
    (batch / "Empty").mkdir()
    context = context_for(client, approved)
    choose_batch(client, tmp_path / "other")
    choose_batch(client, batch)
    response = client.post("/api/archive/cleanups", json={
        "context_id": context["id"], "request_id": "stale-batch",
    })
    assert response.status_code == 409, response.text
    assert (batch / "Empty").is_dir()


def test_cleanup_rejects_arbitrary_delete_paths(client, tmp_path):
    approved = choose_batch(client, tmp_path / "batch")
    context = context_for(client, approved)
    for extra in ({"path": str(tmp_path)}, {"names": [".."]}, {"recursive": True}):
        result = client.post("/api/archive/cleanups", json={
            "context_id": context["id"], "request_id": "invalid", **extra,
        })
        assert result.status_code == 422, result.text
    assert client.post("/api/archive/cleanup-contexts", json={
        "batch_path": str(tmp_path),
    }).status_code == 409


def test_cleanup_endpoints_require_session_and_trusted_origin(tmp_path):
    with TestClient(create_app(tmp_path / "auth.db")) as client:
        for path in ("/api/archive/cleanup-contexts", "/api/archive/cleanups"):
            assert client.post(path, json={}).status_code == 403
            assert client.post(path, json={}, headers={
                "X-Filmpaw-Session": "a" * 64, "Origin": "https://untrusted.example",
            }).status_code == 403
        assert client.get("/api/archive/cleanups/latest").status_code == 403


def test_restart_discards_cleanup_capabilities_without_deleting(tmp_path):
    db_path = tmp_path / "restart.db"
    headers = {"X-Filmpaw-Session": "a" * 64}
    batch = tmp_path / "batch"
    with TestClient(create_app(db_path), headers=headers) as first:
        approved = choose_batch(first, batch)
        (batch / "Empty").mkdir()
        context = context_for(first, approved)
    with TestClient(create_app(db_path), headers=headers) as restarted:
        assert restarted.get("/api/archive/cleanups/latest").json() is None
        response = restarted.post("/api/archive/cleanups", json={
            "context_id": context["id"], "request_id": "old-process",
        })
        assert response.status_code == 409
        assert (batch / "Empty").is_dir()
