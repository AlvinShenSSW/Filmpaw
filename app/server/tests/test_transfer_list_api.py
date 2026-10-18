"""Multi-task APIs preserve exact acceptance, stop sets and log pagination."""

from tests.test_archive_api import seed_pair


def test_list_lookup_stop_set_and_log_contract(client, tmp_path):
    client.app.state.transfers._auto_start = False
    batch, source, _, pid = seed_pair(client, tmp_path)
    context = client.post("/api/archive/contexts", json={
        "local_dir": str(batch), "subdir": "Actor", "performer_id": pid,
    }).json()
    task = client.post("/api/archive/transfers", json={
        "context_id": context["id"], "names": ["Film, 10"], "request_id": "lookup-exact",
    }).json()
    listing = client.get("/api/archive/transfers")
    assert listing.status_code == 200
    assert listing.json()["active"][0]["id"] == task["id"]
    assert listing.json()["busy"] and listing.json()["concurrency"] == 3
    assert listing.json()["blocked_reason"] is None
    assert listing.json()["history"] == []
    assert client.get("/api/archive/transfers/by-request/not-accepted").json() is None
    assert client.get("/api/archive/transfers/by-request/lookup-exact").json()["id"] == task["id"]
    # Validate the entire set before stopping even one existing task.
    bad = client.post("/api/archive/transfers/stop-all", json={"task_ids": [task["id"], "unknown"]})
    assert bad.status_code == 404
    assert client.get(f"/api/archive/transfers/{task['id']}").json()["status"] == "queued"
    for _ in range(2):
        stopped = client.post("/api/archive/transfers/stop-all", json={"task_ids": [task["id"]]})
        assert stopped.status_code == 200
        assert stopped.json() == {"task_ids": [task["id"]]}
    listing = client.get("/api/archive/transfers").json()
    assert not listing["busy"] and not listing["active"]
    assert listing["history"][0]["status"] == "stopped"
    assert (source / "Film, 10").exists()
    logs = client.get("/api/archive/logs", params={"task_id": task["id"], "limit": 1})
    assert logs.status_code == 200
    assert len(logs.json()["items"]) == 1 and logs.json()["next_cursor"] is not None
    row = logs.json()["items"][0]
    assert row["task_id"] == task["id"] and row["kind"] == "transfer"
    assert row["action"] == "transfer_finished" and row["outcome"] == "stopped"
    page = client.get("/api/archive/logs", params={"before_id": row["id"], "limit": 1}).json()
    assert page["items"][0]["id"] < row["id"]
    assert client.get("/api/archive/transfers", params={"limit": 0}).status_code == 422
    assert client.get("/api/archive/logs", params={"before_id": -1}).status_code == 422


def test_new_archive_reads_and_mutations_require_session(client):
    client.headers.pop("X-Filmpaw-Session", None)
    for endpoint in ["/transfers", "/transfers/by-request/any", "/logs"]:
        assert client.get("/api/archive" + endpoint).status_code == 403
    assert client.post("/api/archive/transfers/stop-all", json={"task_ids": []}).status_code == 403
