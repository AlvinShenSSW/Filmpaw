from datetime import datetime, timedelta, timezone
from threading import RLock
from uuid import uuid4

import pytest

from filmpaw_server.db import connect
from filmpaw_server.workflow_index import WorkflowIndex
from filmpaw_server.workflow_models import (
    ActorObservation, ExitProof, IdentityObservation, NativeIdentity, ProbeDraft,
    ProcessBirth, ProcessExit, ProcessOwner, ScanOutcome, ScanSnapshot, WorkflowIndexError,
)


class Harness:
    def __init__(self, db):
        self.now = datetime(2026, 10, 19, tzinfo=timezone.utc)
        self.db, self.lock = db, RLock()
        self.index = WorkflowIndex(db, self.lock, clock=lambda: self.now)

    def identity(self, byte="01"):
        return IdentityObservation(observation_id=str(uuid4()), observed_at=self.now,
                                   identity=NativeIdentity(namespace="native128", volume="1", object_id=byte * 16))

    def start(self, ticket):
        req = self.index.begin_attempt(ticket)
        launcher = ProcessBirth(pid=101, created_filetime_100ns=1001, role="launcher")
        worker = ProcessBirth(pid=102, created_filetime_100ns=1002, role="worker")
        owner = ProcessOwner(runner_instance_id=str(uuid4()), job_name="test-private-job",
                             launcher=launcher, worker=worker, observed_at=self.now)
        self.index.authorize_go(req, owner)
        return req, owner

    def outcome(self, req, owner, *, actors=(), root_identity=None, released=True, status="complete"):
        identity = root_identity or req.expected_root_identity or self.identity()
        binding = {k: getattr(req, k) for k in ("scan_id", "attempt_id", "root_id", "host_id", "export_id",
                                               "root_revision", "mapping_revision", "owner_generation", "kind")}
        snapshot = ScanSnapshot(**binding, observed_from=self.now, observed_until=self.now,
                                root_identity_before=identity, root_identity_after=identity,
                                coverage="complete", actors=actors)
        proof = ExitProof(attempt_id=req.attempt_id, owner_generation=req.owner_generation,
                          processes=tuple(ProcessExit(birth=b, signaled=released)
                                          for b in (owner.launcher, owner.worker)),
                          job_empty=released, io_joined=released, handles_closed=released, checked_at=self.now)
        return ScanOutcome(status=status, owner_state="released" if released else "unknown_owner",
                           snapshot=snapshot, exit_proof=proof)

    def root(self, *, host_id=None, path=None):
        ticket = self.index.reserve_probe(ProbeDraft(private_root_path=path or "//example/" + str(uuid4()),
                                         label="Example", host_id=host_id), request_id=str(uuid4()))
        req, owner = self.start(ticket)
        result = self.index.finish(req, self.outcome(req, owner))
        return ticket.root_id, result.source_id

    def scan(self, root_id, names=("Example Actor",)):
        req, owner = self.start(self.index.reserve_scan(root_id, request_id=str(uuid4())))
        actors = tuple(ActorObservation(relative_actor_path=n, name=n, identity=self.identity("02")) for n in names)
        outcome = self.outcome(req, owner, actors=actors)
        return req, owner, outcome


@pytest.fixture
def h(tmp_path):
    db = connect(tmp_path / "index.db")
    try:
        yield Harness(db)
    finally:
        db.close()


def test_probe_draft_not_visible_and_probe_does_not_claim_index_coverage(h):
    ticket = h.index.reserve_probe(ProbeDraft(private_root_path="//example/share", label="Example"), request_id=str(uuid4()))
    assert h.db.execute("SELECT count(*) FROM sources").fetchone()[0] == 0
    req, owner = h.start(ticket)
    result = h.index.finish(req, h.outcome(req, owner))
    assert result.published and result.source_id
    health = h.index.source_health((result.source_id,))[result.source_id]
    assert health.health == "never_scanned" and health.coverage == "incomplete"
    assert health.probe_health == "fresh" and health.last_success_at is None
    assert h.db.execute("SELECT last_scan_at FROM sources").fetchone()[0] is None


def test_complete_snapshot_updates_legacy_and_workflow_in_one_transaction(h):
    root, source = h.root()
    req, owner, outcome = h.scan(root)
    result = h.index.finish(req, outcome)
    assert (result.published, result.added, result.refreshed, result.missing) == (True, 1, 0, 0)
    assert h.index.get_scan(req.scan_id) == result
    row = h.db.execute("SELECT * FROM performers").fetchone()
    assert row["source_id"] == source and row["name"] == "Example Actor"
    assert h.index.source_health((source,))[source].health == "fresh"


def test_unknown_owner_retained_and_reconciled_without_publishing_cancelled_snapshot(h):
    root, _ = h.root()
    req, owner, outcome = h.scan(root)
    result = h.index.finish(req, h.outcome(req, owner, released=False))
    assert result.owner_state == "unknown_owner" and not result.published
    assert len(h.index.pending_owners()) == 1
    same = h.index.reserve_scan(root, request_id=str(uuid4()))
    assert same.scan_id == req.scan_id
    h.index.cancel_publication(same, reason="timeout")
    result = h.index.finish(req, outcome)
    assert result.owner_state == "released" and not result.published
    assert not h.index.pending_owners()
    assert h.db.execute("SELECT count(*) FROM performers").fetchone()[0] == 0


def test_exit_proof_must_cover_all_observed_births(h):
    root, _ = h.root()
    req, owner, outcome = h.scan(root)
    proof = outcome.exit_proof.model_copy(update={"processes": outcome.exit_proof.processes[:1]})
    result = h.index.finish(req, outcome.model_copy(update={"exit_proof": proof}))
    assert result.owner_state == "unknown_owner" and not result.published


def test_partial_snapshot_does_not_mark_missing_or_advance_success(h):
    root, source = h.root()
    req, owner, outcome = h.scan(root)
    h.index.finish(req, outcome)
    before = h.index.source_health((source,))[source].last_success_at
    req, owner, outcome = h.scan(root, ())
    h.index.finish(req, outcome.model_copy(update={"status": "partial"}))
    assert h.db.execute("SELECT is_missing FROM performers").fetchone()[0] == 0
    assert h.index.source_health((source,))[source].last_success_at == before


def test_source_delete_cas_preserves_owner_and_history_and_reused_source_id_is_new_root(h):
    root, source = h.root()
    req, owner, outcome = h.scan(root)
    with h.lock, h.db:
        h.index.tombstone_source_tx(source)
        h.db.execute("DELETE FROM sources WHERE id=?", (source,))
    result = h.index.finish(req, outcome)
    assert not result.published and result.error_code == "stale_binding"
    assert h.db.execute("SELECT count(*) FROM sources").fetchone()[0] == 0
    root2, source2 = h.root()
    assert source2 == source and root2 != root


def test_per_host_unknown_and_global_owner_bounds(h):
    hosts = [str(uuid4()) for _ in range(5)]
    roots = [h.root(host_id=host)[0] for host in hosts]
    requests = [h.scan(root) for root in roots[:4]]
    with pytest.raises(WorkflowIndexError, match="busy"):
        h.index.begin_attempt(h.index.reserve_scan(roots[4], request_id=str(uuid4())))
    req, owner, _ = requests[0]
    h.index.finish(req, h.outcome(req, owner, released=False))
    with pytest.raises(WorkflowIndexError, match="busy"):
        h.index.begin_attempt(h.index.reserve_scan(roots[4], request_id=str(uuid4())))


def test_permission_failure_requires_explicit_retry_and_retains_cache(h):
    root, source = h.root()
    req, owner, outcome = h.scan(root)
    failed = outcome.model_copy(update={"status": "failed", "snapshot": None, "error_code": "permission_denied"})
    h.index.finish(req, failed)
    assert h.index.source_health((source,))[source].health == "permission_denied"
    with pytest.raises(WorkflowIndexError, match="configuration_required"):
        h.index.begin_attempt(h.index.reserve_scan(root, request_id=str(uuid4())))


def test_index_restart_keeps_unknown_owner(h):
    root, _ = h.root()
    req, owner, _ = h.scan(root)
    reopened = WorkflowIndex(h.db, h.lock, clock=lambda: h.now)
    pending = reopened.pending_owners()
    assert len(pending) == 1 and pending[0].state == "unknown_owner"
    assert pending[0].owner == owner and pending[0].request == req


def test_failed_hello_can_release_captured_launcher_but_never_authorize_go(h):
    root, _ = h.root()
    ticket = h.index.reserve_scan(root, request_id=str(uuid4()))
    req = h.index.begin_attempt(ticket)
    launcher = ProcessBirth(pid=201, created_filetime_100ns=2001, role="launcher")
    owner = ProcessOwner(runner_instance_id=str(uuid4()), job_name="test-job", launcher=launcher, observed_at=h.now)
    h.index.update_owner(req, owner)
    with pytest.raises(WorkflowIndexError, match="worker_unverified"):
        h.index.authorize_go(req, owner)
    proof = ExitProof(attempt_id=req.attempt_id, owner_generation=req.owner_generation,
                      processes=(ProcessExit(birth=launcher, signaled=True),), job_empty=True,
                      io_joined=True, handles_closed=True, checked_at=h.now)
    result = h.index.finish(req, ScanOutcome(status="failed", owner_state="released", exit_proof=proof, error_code="protocol_error"))
    assert result.owner_state == "released" and not result.published


def test_hello_cannot_erase_prior_uncertain_sampling(h):
    root, _ = h.root()
    ticket = h.index.reserve_scan(root, request_id=str(uuid4()))
    req = h.index.begin_attempt(ticket)
    launcher = ProcessBirth(pid=201, created_filetime_100ns=2001, role="launcher")
    prior = ProcessOwner(runner_instance_id=str(uuid4()), job_name="test-job", launcher=launcher,
                         observed_at=h.now, identity_uncertain=True, unresolved_member_pids=(202,))
    h.index.update_owner(req, prior)
    worker = ProcessBirth(pid=203, created_filetime_100ns=2003, role="worker")
    clean = prior.model_copy(update={"worker": worker, "identity_uncertain": False, "unresolved_member_pids": ()})
    with pytest.raises(WorkflowIndexError, match="worker_unverified"):
        h.index.authorize_go(req, clean)
    assert h.index.pending_owners()[0].owner.identity_uncertain


def test_remap_during_unknown_retains_original_host_and_fences_late_result(h):
    old_host, new_host = str(uuid4()), str(uuid4())
    root, _ = h.root(host_id=old_host)
    req, owner, outcome = h.scan(root)
    h.index.finish(req, h.outcome(req, owner, released=False))
    revision = h.index.update_root_mapping(root, expected_revision=1,
        draft=ProbeDraft(private_root_path="//different-example/share", label="Changed", host_id=new_host))
    assert revision == 2
    pending = h.index.pending_owners()[0]
    assert pending.request.host_id == old_host
    assert h.index.reserve_scan(root, request_id=str(uuid4())).scan_id == req.scan_id
    settled = h.index.finish(req, outcome)
    assert not settled.published and settled.error_code == "stale_binding"
    assert not h.index.pending_owners()
    with pytest.raises(WorkflowIndexError, match="revision_conflict"):
        h.index.update_root_mapping(root, expected_revision=1,
            draft=ProbeDraft(private_root_path="//different-example/share", label="Changed"))


def test_full_snapshot_sql_failure_rolls_back_all_legacy_and_index_changes(h, monkeypatch):
    import filmpaw_server.scan as scan
    root, source = h.root()
    req, _, outcome = h.scan(root, ("Actor A", "Actor B"))
    original = scan._apply_thumbnail
    calls = 0
    def fail_second(conn, pid, observation):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("synthetic SQL apply failure")
        original(conn, pid, observation)
    monkeypatch.setattr(scan, "_apply_thumbnail", fail_second)
    with pytest.raises(RuntimeError):
        h.index.finish(req, outcome)
    assert not h.db.in_transaction
    assert h.db.execute("SELECT count(*) FROM performers").fetchone()[0] == 0
    assert h.db.execute("SELECT count(*) FROM workflow_locations").fetchone()[0] == 0
    assert h.index.source_health((source,))[source].last_success_at is None
    assert len(h.index.pending_owners()) == 1


def test_backoff_half_open_probe_and_refresh_do_not_drop_owner(h):
    root, _ = h.root()
    assert root in h.index.refresh_roots()
    for delay in (30, 120, 900):
        req, owner, outcome = h.scan(root)
        h.index.finish(req, outcome.model_copy(update={"status": "failed", "snapshot": None, "error_code": "offline"}))
        ticket = h.index.reserve_scan(root, request_id=str(uuid4()))
        assert ticket in h.index.queued_tickets()
        with pytest.raises(WorkflowIndexError, match="backoff"):
            h.index.begin_attempt(ticket)
        h.index.cancel_publication(ticket)
        h.now += timedelta(seconds=delay)
    assert root not in h.index.refresh_roots()
    assert h.index.probe_roots() == (root,)
    req, owner = h.start(h.index.reserve_scan(root, kind="probe_root", request_id=str(uuid4())))
    h.index.finish(req, h.outcome(req, owner))
    assert not h.index.probe_roots() and root in h.index.refresh_roots()


def test_distinct_native_directory_replacement_creates_new_location_history(h):
    root, _ = h.root()
    req, _, outcome = h.scan(root)
    h.index.finish(req, outcome)
    old = h.db.execute("SELECT location_id FROM workflow_locations").fetchone()[0]
    req, _, outcome = h.scan(root)
    changed_actor = outcome.snapshot.actors[0].model_copy(update={"identity": h.identity("03")})
    outcome = outcome.model_copy(update={"snapshot": outcome.snapshot.model_copy(update={"actors": (changed_actor,)})})
    h.index.finish(req, outcome)
    rows = h.db.execute("SELECT location_id,retired FROM workflow_locations").fetchall()
    assert len(rows) == 2
    assert next(r for r in rows if r["location_id"] == old)["retired"] == 1


def test_root_identity_change_blocks_publication_and_invalidates_revision(h):
    root, source = h.root()
    req, owner, _ = h.scan(root)
    result = h.index.finish(req, h.outcome(req, owner, root_identity=h.identity("04")))
    assert not result.published and result.error_code == "identity_changed"
    assert h.index.source_health((source,))[source].root_revision == 2


def test_source_and_root_queue_bounds_are_independent_of_duplicate_requests(h):
    from filmpaw_server.workflow_models import ScanProfile
    roots = [h.root()[0] for _ in range(3)]
    h.index.profile = ScanProfile(queue_limit=2)
    first = h.index.reserve_scan(roots[0], request_id=str(uuid4()))
    assert h.index.reserve_scan(roots[0], request_id=str(uuid4())) == first
    h.index.reserve_scan(roots[1], request_id=str(uuid4()))
    with pytest.raises(WorkflowIndexError, match="busy"):
        h.index.reserve_scan(roots[2], request_id=str(uuid4()))
    assert len(h.index.queued_tickets()) == 2


def open_circuit(h, root, *, advance_last=True):
    """Produce the real failure/backoff sequence through the repository API."""
    for delay in (30, 120, 900):
        req, _, outcome = h.scan(root)
        h.index.finish(req, outcome.model_copy(update={
            "status": "failed", "snapshot": None, "error_code": "offline",
        }))
        if delay != 900 or advance_last:
            h.now += timedelta(seconds=delay)


def authorize_request(h, request):
    owner = ProcessOwner(
        runner_instance_id=str(uuid4()), job_name="test-recovery-job",
        launcher=ProcessBirth(pid=301, created_filetime_100ns=3001, role="launcher"),
        worker=ProcessBirth(pid=302, created_filetime_100ns=3002, role="worker"),
        observed_at=h.now,
    )
    h.index.authorize_go(request, owner)
    return owner


def test_full_queue_cannot_be_coalesced_into_a_probe_success(h):
    root, _ = h.root()
    open_circuit(h, root)
    full = h.index.reserve_scan(root, request_id=str(uuid4()))
    with pytest.raises(WorkflowIndexError, match="busy"):
        h.index.reserve_scan(root, kind="probe_root", request_id=str(uuid4()))
    assert h.index.queued_tickets() == (full,)


def test_manual_full_keeps_its_ticket_while_independent_recovery_probe_runs(h):
    root, source = h.root()
    open_circuit(h, root)
    request_id = str(uuid4())
    full = h.index.reserve_scan(root, request_id=request_id)
    with pytest.raises(WorkflowIndexError, match="probe_required"):
        h.index.begin_attempt(full)
    probe = h.index.start_recovery_probe(full)
    assert probe.kind == "probe_root" and probe.root_id == full.root_id
    assert probe.scan_id != full.scan_id and probe.attempt_id != full.attempt_id
    assert h.index.queued_tickets() == (full,)
    assert h.index.get_scan(probe.scan_id).status == "starting"
    assert h.index.get_scan(probe.scan_id).owner_state == "owned"
    assert h.index.reserve_scan(root, request_id=request_id) == full
    with pytest.raises(WorkflowIndexError, match="request_conflict"):
        h.index.reserve_scan(root, kind="probe_root", request_id=request_id)
    with pytest.raises(WorkflowIndexError, match="busy"):
        h.index.start_recovery_probe(full)
    owner = authorize_request(h, probe)
    assert h.index.finish(probe, h.outcome(probe, owner)).published
    assert h.index.get_scan(full.scan_id).status == "queued"
    assert h.index.source_health((source,))[source].last_success_at is None
    request, owner = h.start(full)
    actor = ActorObservation(relative_actor_path="Example Actor", name="Example Actor", identity=h.identity("02"))
    result = h.index.finish(request, h.outcome(request, owner, actors=(actor,)))
    assert result.scan_id == full.scan_id and result.attempt_id == full.attempt_id
    assert result.published and result.added == 1


def test_owned_probe_only_coalesces_probes_and_allows_one_full_queued(h):
    root, _ = h.root()
    probe = h.index.reserve_scan(root, kind="probe_root", request_id=str(uuid4()))
    h.start(probe)
    full = h.index.reserve_scan(root, request_id=str(uuid4()))
    assert full.kind == "scan_index" and full.scan_id != probe.scan_id
    assert h.index.reserve_scan(root, request_id=str(uuid4())) == full
    assert h.index.reserve_scan(root, kind="probe_root", request_id=str(uuid4())) == probe
    assert h.index.queued_tickets() == (full,)
    with pytest.raises(WorkflowIndexError, match="busy"):
        h.index.begin_attempt(full)


@pytest.mark.parametrize("guard,code", [
    ("backoff", "backoff"), ("cancelled", "attempt_already_started"),
    ("remapped", "stale_binding"), ("closed", "probe_not_required"),
])
def test_recovery_probe_respects_original_full_admission(h, guard, code):
    root, _ = h.root()
    if guard != "closed":
        open_circuit(h, root, advance_last=guard != "backoff")
    full = h.index.reserve_scan(root, request_id=str(uuid4()))
    if guard == "cancelled":
        h.index.cancel_publication(full, reason="timeout")
    elif guard == "remapped":
        h.index.update_root_mapping(root, expected_revision=1, draft=ProbeDraft(
            private_root_path="//changed-example/share", label="Changed", host_id=str(uuid4()),
        ))
    before = h.db.execute("SELECT count(*) FROM workflow_scan_operations").fetchone()[0]
    with pytest.raises(WorkflowIndexError, match=code):
        h.index.start_recovery_probe(full)
    assert h.db.execute("SELECT count(*) FROM workflow_scan_operations").fetchone()[0] == before
    assert not h.index.pending_owners()


@pytest.mark.parametrize("failure,code", [("offline", "backoff"), ("permission_denied", "configuration_required"), ("unknown", "busy")])
def test_recovery_probe_failure_keeps_full_and_respects_host_state(h, failure, code):
    root, _ = h.root()
    open_circuit(h, root)
    full = h.index.reserve_scan(root, request_id=str(uuid4()))
    probe = h.index.start_recovery_probe(full)
    owner = authorize_request(h, probe)
    outcome = h.outcome(probe, owner, released=failure != "unknown")
    outcome = outcome.model_copy(update={"status": "failed", "snapshot": None,
                                         "error_code": "offline" if failure == "unknown" else failure})
    result = h.index.finish(probe, outcome)
    assert not result.published and h.index.queued_tickets() == (full,)
    with pytest.raises(WorkflowIndexError, match=code):
        h.index.start_recovery_probe(full)
    if failure == "unknown":
        assert result.owner_state == "unknown_owner" and len(h.index.pending_owners()) == 1
    elif failure == "offline":
        h.now += timedelta(seconds=900)
        second = h.index.start_recovery_probe(full)
        assert second.scan_id != probe.scan_id and second.owner_generation > probe.owner_generation


def test_recovery_probe_does_not_bypass_global_owner_limit(h):
    root, _ = h.root(host_id=str(uuid4()))
    roots = [h.root(host_id=str(uuid4()))[0] for _ in range(4)]
    open_circuit(h, root)
    full = h.index.reserve_scan(root, request_id=str(uuid4()))
    for other in roots:
        h.scan(other)
    with pytest.raises(WorkflowIndexError, match="busy"):
        h.index.start_recovery_probe(full)
    assert len(h.index.pending_owners()) == 4 and h.index.queued_tickets() == (full,)


def test_cancel_queued_timeout_returns_durable_result_and_prevents_start(h):
    root, _ = h.root()
    ticket = h.index.reserve_scan(root, request_id=str(uuid4()))
    result = h.index.cancel_publication(ticket, reason="timeout")
    assert result.status == "cancelled" and result.error_code == "timeout"
    assert result.owner_state == "none" and not result.published
    assert h.index.get_scan(ticket.scan_id) == result
    assert h.index.cancel_publication(ticket) == result
    with pytest.raises(WorkflowIndexError, match="attempt_already_started"):
        h.index.begin_attempt(ticket)


@pytest.mark.parametrize("unknown", [False, True])
def test_cancel_active_returns_current_facts_retains_owner_and_fences_late_publish(h, unknown):
    root, _ = h.root()
    ticket = h.index.reserve_scan(root, request_id=str(uuid4()))
    request, owner = h.start(ticket)
    if unknown:
        h.index.finish(request, h.outcome(request, owner, released=False))
    before = h.index.get_scan(ticket.scan_id)
    cancelled = h.index.cancel_publication(ticket, reason="timeout")
    assert cancelled == before
    assert cancelled.owner_state == ("unknown_owner" if unknown else "owned")
    assert len(h.index.pending_owners()) == 1
    late = h.index.finish(request, h.outcome(request, owner))
    assert not late.published and late.owner_state == "released"


def test_cancel_after_completion_returns_success_without_reversing_publication(h):
    root, _ = h.root()
    ticket = h.index.reserve_scan(root, request_id=str(uuid4()))
    request, owner = h.start(ticket)
    completed = h.index.finish(request, h.outcome(request, owner))
    assert completed.published
    assert h.index.cancel_publication(ticket, reason="timeout") == completed
    assert h.index.get_scan(ticket.scan_id) == completed
    row = h.db.execute("SELECT publication_allowed FROM workflow_scan_operations WHERE scan_id=?", (ticket.scan_id,)).fetchone()
    assert row[0] == 1
