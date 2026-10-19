"""R1: a root configuration failure cannot poison the shared host slot."""

from datetime import timedelta
from uuid import uuid4

import pytest

from filmpaw_server.workflow_index import WorkflowIndex
from filmpaw_server.workflow_models import ProbeDraft, WorkflowIndexError
from tests.test_workflow_index_state import Harness, h, open_circuit  # noqa: F401


def fail(h, root, code, *, manual=False):
    ticket = h.index.reserve_scan(root, request_id=str(uuid4()), **({"manual": True} if manual else {}))
    request, owner = h.start(ticket)
    result = h.index.finish(request, h.outcome(request, owner).model_copy(
        update={"status": "failed", "snapshot": None, "error_code": code}))
    assert result.owner_state == "released" and not result.published
    return request


@pytest.mark.parametrize("code", ["permission_denied", "identity_unknown", "identity_changed", "not_directory"])
def test_configuration_failure_only_pauses_affected_root(h, code):
    broken, broken_source = h.root()
    healthy, healthy_source = h.root()
    request, _, outcome = h.scan(healthy)
    h.index.finish(request, outcome)
    before = h.index.source_health((healthy_source,))[healthy_source]
    fail(h, broken, code)
    request, _, outcome = h.scan(healthy)
    assert h.index.finish(request, outcome).published
    assert h.index.source_health((healthy_source,))[healthy_source].last_success_at == before.last_success_at
    assert broken not in h.index.refresh_roots()
    assert broken not in h.index.probe_roots()
    with pytest.raises(WorkflowIndexError, match="configuration_required"):
        h.index.reserve_scan(broken, request_id=str(uuid4()))
    assert h.index.source_health((broken_source,))[broken_source].coverage == "incomplete"
    host = h.db.execute("SELECT failures,circuit,next_probe_at FROM workflow_host_state").fetchone()
    assert tuple(host) == (0, "closed", None)


@pytest.mark.parametrize("code", ["permission_denied", "identity_unknown", "identity_changed", "not_directory"])
def test_manual_retry_is_persistent_one_attempt_and_success_unpauses_only_its_root(h, code):
    root, _ = h.root()
    other, _ = h.root()
    fail(h, root, code)
    fail(h, other, "permission_denied")
    request_id = str(uuid4())
    ticket = h.index.reserve_scan(root, request_id=request_id, manual=True)
    assert h.index.reserve_scan(root, request_id=request_id, manual=True) == ticket
    with pytest.raises(WorkflowIndexError, match="request_conflict"):
        h.index.reserve_scan(root, request_id=request_id)
    # Reopen the repository before dispatch: manual intent is not an in-memory flag.
    h.index = WorkflowIndex(h.db, h.lock, clock=lambda: h.now)
    assert root not in h.index.refresh_roots()
    request, owner = h.start(ticket)
    result = h.index.finish(request, h.outcome(request, owner))
    assert result.published
    assert root in h.index.refresh_roots() or h.index.source_health((result.source_id,))[result.source_id].health == "fresh"
    with pytest.raises(WorkflowIndexError, match="configuration_required"):
        h.index.reserve_scan(other, request_id=str(uuid4()))


@pytest.mark.parametrize("ending", ["cancelled", "failed", "queued_cancel"])
def test_manual_retry_without_verified_success_does_not_clear_pause(h, ending):
    root, _ = h.root()
    fail(h, root, "permission_denied")
    ticket = h.index.reserve_scan(root, request_id=str(uuid4()), manual=True)
    if ending == "queued_cancel":
        h.index.cancel_publication(ticket, reason="timeout")
    else:
        request, owner = h.start(ticket)
        h.index.finish(request, h.outcome(request, owner).model_copy(update={
            "status": ending, "snapshot": None,
            "error_code": "cancelled" if ending == "cancelled" else "offline"}))
    h.index = WorkflowIndex(h.db, h.lock, clock=lambda: h.now)
    h.now += timedelta(days=1)
    assert root not in h.index.refresh_roots() and root not in h.index.probe_roots()
    with pytest.raises(WorkflowIndexError, match="configuration_required"):
        h.index.reserve_scan(root, request_id=str(uuid4()))


def test_failure_cancels_older_queued_intent_but_keeps_other_roots(h):
    root, _ = h.root()
    other, _ = h.root()
    probe = h.index.reserve_scan(root, kind="probe_root", request_id=str(uuid4()))
    request, owner = h.start(probe)
    queued = h.index.reserve_scan(root, request_id=str(uuid4()), manual=True)
    unrelated = h.index.reserve_scan(other, request_id=str(uuid4()))
    h.index.finish(request, h.outcome(request, owner).model_copy(update={
        "status": "failed", "snapshot": None, "error_code": "permission_denied"}))
    assert h.index.get_scan(queued.scan_id).status == "cancelled"
    assert h.index.get_scan(queued.scan_id).error_code == "permission_denied"
    assert h.index.queued_tickets() == (unrelated,)
    with pytest.raises(WorkflowIndexError, match="attempt_already_started"):
        h.index.begin_attempt(queued)


def test_manual_retry_cannot_clear_unknown_owner_or_accept_changed_identity(h):
    root, _ = h.root()
    other, _ = h.root()
    fail(h, root, "permission_denied")
    ticket = h.index.reserve_scan(root, request_id=str(uuid4()), manual=True)
    request, owner = h.start(ticket)
    h.index.finish(request, h.outcome(request, owner, released=False))
    assert h.index.reserve_scan(root, request_id=str(uuid4()), manual=True) == ticket
    other_ticket = h.index.reserve_scan(other, request_id=str(uuid4()), manual=True)
    with pytest.raises(WorkflowIndexError, match="busy"):
        h.index.begin_attempt(other_ticket)
    changed = h.outcome(request, owner, root_identity=h.identity("ab"))
    result = h.index.finish(request, changed)
    assert result.owner_state == "released" and not result.published
    assert result.error_code == "identity_changed"
    next_ticket = h.index.reserve_scan(root, request_id=str(uuid4()), manual=True)
    request, owner = h.start(next_ticket)
    result = h.index.finish(request, h.outcome(request, owner, root_identity=h.identity("ab")))
    assert not result.published and result.error_code == "identity_changed"


def test_failed_draft_retries_without_poisoning_new_add_or_host_circuit(h):
    draft = ProbeDraft(private_root_path="//example/missing", label="Missing")
    for code in ("not_directory", "permission_denied", "identity_unknown"):
        ticket = h.index.reserve_probe(draft, request_id=str(uuid4()), manual=True)
        request, owner = h.start(ticket)
        h.index.finish(request, h.outcome(request, owner).model_copy(update={
            "status": "failed", "snapshot": None, "error_code": code}))
        root, _ = h.root()
        assert root != ticket.root_id
    ticket = h.index.reserve_probe(draft, request_id=str(uuid4()), manual=True)
    request, owner = h.start(ticket)
    assert h.index.finish(request, h.outcome(request, owner)).published


def test_real_host_backoff_still_applies_to_manual_retry_and_recovery_probe(h):
    root, _ = h.root()
    open_circuit(h, root, advance_last=False)
    ticket = h.index.reserve_scan(root, request_id=str(uuid4()), manual=True)
    with pytest.raises(WorkflowIndexError, match="backoff"):
        h.index.start_recovery_probe(ticket)
    h.now += timedelta(seconds=901)
    request = h.index.start_recovery_probe(ticket)
    row = h.db.execute("SELECT manual_intent FROM workflow_scan_operations WHERE scan_id=?", (request.scan_id,)).fetchone()
    assert row[0] == 1


def test_permission_during_half_open_does_not_strand_other_roots(h):
    root, _ = h.root()
    healthy, _ = h.root()
    open_circuit(h, root)
    full = h.index.reserve_scan(root, request_id=str(uuid4()))
    probe = h.index.start_recovery_probe(full)
    from tests.test_workflow_index_state import authorize_request
    owner = authorize_request(h, probe)
    h.index.finish(probe, h.outcome(probe, owner).model_copy(update={
        "status": "failed", "snapshot": None, "error_code": "permission_denied"}))
    assert root not in h.index.probe_roots()
    assert healthy in h.index.probe_roots()
    assert h.index.get_scan(full.scan_id).status == "cancelled"


def test_delete_readd_creates_new_identity_without_reviving_old_root(h):
    path = "//example/replaced"
    root, source = h.root(path=path)
    fail(h, root, "identity_changed")
    with h.lock, h.db:
        h.index.tombstone_source_tx(source)
        h.db.execute("DELETE FROM sources WHERE id=?", (source,))
    new = h.index.reserve_probe(ProbeDraft(private_root_path=path, label="Replacement"),
                                request_id=str(uuid4()), manual=True)
    assert new.root_id != root
    request, owner = h.start(new)
    assert request.expected_root_identity is None
    assert h.index.finish(request, h.outcome(request, owner, root_identity=h.identity("ab"))).published
    old = h.db.execute("SELECT tombstoned,configuration_reason FROM workflow_roots WHERE root_id=?", (root,)).fetchone()
    assert tuple(old) == (1, "identity_changed")


def test_authenticated_source_routes_supply_manual_intent(client, source_dir, monkeypatch):
    scans, index = client.app.state.scans, client.app.state.workflow_index
    scans._stop.set()
    scans._wake.set()
    scans._thread.join(2)
    assert not scans._thread.is_alive()
    harness = Harness.__new__(Harness)
    harness.db, harness.lock, harness.index, harness.now = index.conn, index.lock, index, index.clock()
    seen = []
    original_probe, original_scan = scans.request_probe, scans.request
    def probe(draft, **kwargs):
        seen.append(("probe", kwargs.get("manual")))
        return original_probe(draft, **kwargs)
    def scan(root, **kwargs):
        seen.append(("scan", kwargs.get("manual")))
        return original_scan(root, **kwargs)
    async def wait(ticket, *, deadline):
        request, owner = harness.start(ticket)
        return index.finish(request, harness.outcome(request, owner))
    monkeypatch.setattr(scans, "request_probe", probe)
    monkeypatch.setattr(scans, "request", scan)
    monkeypatch.setattr(scans, "wait", wait)
    added = client.post("/api/sources", json={"unc_path": str(source_dir)})
    assert added.status_code == 201, added.text
    source = added.json()["id"]
    fail(harness, index.root_for_source(source), "permission_denied")
    assert client.post(f"/api/sources/{source}/scan").status_code == 200
    fail(harness, index.root_for_source(source), "permission_denied")
    assert client.post("/api/scan-all").json()[0]["ok"] is True
    assert seen == [("probe", True), ("scan", True), ("scan", True)]
    before = len(seen)
    assert client.post(f"/api/sources/{source}/scan", headers={"X-Filmpaw-Session": "wrong"}).status_code == 403
    assert len(seen) == before
