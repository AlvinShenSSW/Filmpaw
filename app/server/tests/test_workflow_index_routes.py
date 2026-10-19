"""Actual legacy route consumers use cached health and retain index history."""

import threading
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest

from filmpaw_server.workflow_models import WorkflowIndexError
from tests.conftest import add_performer_folder


def add_scanned_source(client, source_dir):
    add_performer_folder(source_dir, "Fixture actor")
    added = client.post("/api/sources", json={"unc_path": str(source_dir)})
    assert added.status_code == 201, added.text
    sid = added.json()["id"]
    scanned = client.post(f"/api/sources/{sid}/scan")
    assert scanned.status_code == 200, scanned.text
    return sid


def test_source_list_does_not_probe_paths_and_probe_is_not_index_coverage(client, source_dir, monkeypatch):
    result = client.post("/api/sources", json={"unc_path": str(source_dir)})
    assert result.status_code == 201, result.text

    def forbid(*_args, **_kwargs):
        pytest.fail("source listing must not probe a NAS path")

    # Existing GET /sources used isdir for every source. None of this request's
    # work should now touch the filesystem, even before the first full scan.
    monkeypatch.setattr("os.path.isdir", forbid)
    response = client.get("/api/sources")
    assert response.status_code == 200
    row = response.json()[0]
    assert row["health_state"] == "never_scanned"
    assert row["last_success_at"] is None and row["observed_at"] is not None


def test_delete_source_tombstones_workflow_history(client, source_dir):
    sid = add_scanned_source(client, source_dir)
    index = client.app.state.workflow_index
    root_id = index.root_for_source(sid)
    with index.lock:
        ids = [r[0] for r in index.conn.execute("SELECT location_id FROM workflow_locations WHERE root_id=?", (root_id,))]
    assert ids
    assert client.delete(f"/api/sources/{sid}").status_code == 204
    with index.lock:
        root = index.conn.execute("SELECT * FROM workflow_roots WHERE root_id=?", (root_id,)).fetchone()
        assert root["tombstoned"] and root["legacy_source_id"] is None
        retained = [r[0] for r in index.conn.execute("SELECT location_id FROM workflow_locations WHERE root_id=?", (root_id,))]
        assert retained == ids
        assert index.conn.execute("SELECT count(*) FROM performers WHERE source_id=?", (sid,)).fetchone()[0] == 0


def test_alias_route_changes_revision_and_failure_rolls_back_both_records(client, source_dir, monkeypatch):
    add_scanned_source(client, source_dir)
    pid = client.get("/api/performers").json()["items"][0]["id"]
    index = client.app.state.workflow_index
    def revision():
        with index.lock:
            return index.conn.execute("SELECT index_revision FROM workflow_index_meta").fetchone()[0]
    initial = revision()
    response = client.post(f"/api/performers/{pid}/aliases", json={"alias": "Fixture alias"})
    assert response.status_code == 201
    assert revision() > initial
    current = revision()
    actual = index.aliases_changed_tx
    def fail(group):
        actual(group)
        raise WorkflowIndexError("storage_error", 503)
    monkeypatch.setattr(index, "aliases_changed_tx", fail)
    response = client.post(f"/api/performers/{pid}/aliases", json={"alias": "Uncommitted alias"})
    assert response.status_code == 503
    assert revision() == current
    with index.lock:
        assert not index.conn.in_transaction
        assert index.conn.execute("SELECT count(*) FROM aliases WHERE alias='Uncommitted alias'").fetchone()[0] == 0


def test_cached_queries_remain_available_while_a_scan_waits(client, source_dir, monkeypatch):
    sid = add_scanned_source(client, source_dir)
    entered, release = threading.Event(), threading.Event()
    original_wait = client.app.state.scans.wait
    async def held_wait(ticket, *, deadline):
        import asyncio
        entered.set()
        await asyncio.to_thread(release.wait, 2)
        return await original_wait(ticket, deadline=deadline)
    monkeypatch.setattr(client.app.state.scans, "wait", held_wait)
    with ThreadPoolExecutor(max_workers=2) as pool:
        pending = pool.submit(client.post, f"/api/sources/{sid}/scan")
        try:
            assert entered.wait(2)
            read = pool.submit(client.get, "/api/sources")
            assert read.result(timeout=1).status_code == 200
            assert client.get("/api/performers").status_code == 200
            assert client.get("/api/health").status_code == 200
        finally:
            release.set()
        assert pending.result(timeout=5).status_code == 200


def test_scanner_exit_refusal_uses_existing_error_protocol_without_latching_transfers(client, monkeypatch):
    seen = []
    def pending(_callback):
        seen.append(True)
        raise WorkflowIndexError("scan_exit_pending", 409)
    monkeypatch.setattr(client.app.state.scans, "prepare_exit", pending)
    response = client.post("/api/archive/prepare-exit")
    assert response.status_code == 409
    assert response.json()["detail"] == "扫描尚未安全结束，已保留窗口，请稍后重试"
    assert seen and not client.app.state.transfers._closing


@pytest.mark.parametrize("state", ["queued", "running", "unknown"])
def test_failed_route_wait_cancels_late_snapshot_publication(client, source_dir, monkeypatch, state):
    from tests.test_workflow_index_state import Harness

    added = client.post("/api/sources", json={"unc_path": str(source_dir)})
    assert added.status_code == 201, added.text
    scans, index = client.app.state.scans, client.app.state.workflow_index
    scans._stop.set()
    scans._wake.set()
    scans._thread.join(2)
    assert not scans._thread.is_alive()
    h = Harness.__new__(Harness)
    h.db, h.lock, h.index, h.now = index.conn, index.lock, index, index.clock()
    retained = {}

    async def expired_wait(ticket, *, deadline):
        retained["ticket"] = ticket
        if state != "queued":
            req, owner = h.start(ticket)
            retained.update(request=req, owner=owner)
            if state == "unknown":
                index.finish(req, h.outcome(req, owner, released=False))
        return index.get_scan(ticket.scan_id)

    monkeypatch.setattr(scans, "wait", expired_wait)
    response = client.post(f"/api/sources/{added.json()['id']}/scan")
    assert response.status_code == 503, response.text
    ticket = retained["ticket"]
    try:
        row = index.conn.execute("SELECT publication_allowed FROM workflow_scan_operations WHERE scan_id=?", (ticket.scan_id,)).fetchone()
        assert row[0] == 0
        if state == "queued":
            with pytest.raises(WorkflowIndexError, match="attempt_already_started"):
                index.begin_attempt(ticket)
        else:
            assert index.pending_owners()
            result = index.finish(retained["request"], h.outcome(retained["request"], retained["owner"]))
            assert not result.published and not index.pending_owners()
        assert index.conn.execute("SELECT last_scan_at FROM sources WHERE id=?", (added.json()["id"],)).fetchone()[0] is None
    finally:
        if state != "queued" and index.pending_owners():
            # Synthetic owner has no native resources; close its fixture fact.
            index.finish(retained["request"], h.outcome(retained["request"], retained["owner"]))


def test_completion_winning_timeout_cancellation_returns_its_real_success(client, source_dir, monkeypatch):
    from tests.test_workflow_index_state import Harness

    added = client.post("/api/sources", json={"unc_path": str(source_dir)})
    assert added.status_code == 201, added.text
    scans, index = client.app.state.scans, client.app.state.workflow_index
    scans._stop.set()
    scans._wake.set()
    scans._thread.join(2)
    h = Harness.__new__(Harness)
    h.db, h.lock, h.index, h.now = index.conn, index.lock, index, index.clock()
    original_cancel, retained = index.cancel_publication, {}

    async def expired_wait(ticket, *, deadline):
        retained["ticket"] = ticket
        return index.get_scan(ticket.scan_id)

    def completed_before_cancel(ticket, *, reason):
        req, owner = h.start(ticket)
        index.finish(req, h.outcome(req, owner))
        return original_cancel(ticket, reason=reason)

    monkeypatch.setattr(scans, "wait", expired_wait)
    monkeypatch.setattr(index, "cancel_publication", completed_before_cancel)
    response = client.post(f"/api/sources/{added.json()['id']}/scan")
    assert response.status_code == 200, response.text
    assert index.get_scan(retained["ticket"].scan_id).published
