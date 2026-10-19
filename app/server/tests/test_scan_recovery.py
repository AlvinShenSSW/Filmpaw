from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
from uuid import uuid4

import pytest

from filmpaw_server.scan_recovery import RecoveryInspection
from filmpaw_server.workflow_models import (
    PendingOwner, ProcessBirth, ProcessOwner, RuntimeIdentity, ScanProfile,
    ScanRequest, digest,
    ProbeDraft, ScanOutcome, WorkflowIndexError,
)


CRASH_PARENT = r'''
import json,os,sys,threading
from datetime import datetime,timezone
from pathlib import Path
from uuid import uuid4
from filmpaw_server.db import connect
from filmpaw_server.session import library_session
from filmpaw_server.scan_native import NativeOwner,WindowsAPI
from filmpaw_server.scan_process import _launch
from filmpaw_server.scan_protocol import encode_frame
from filmpaw_server.workflow_index import WorkflowIndex
from filmpaw_server.workflow_models import ProbeDraft,ProcessBirth,ProcessOwner,RuntimeIdentity
with library_session(Path(sys.argv[1])):
    db=connect(Path(sys.argv[1])); index=WorkflowIndex(db,threading.RLock())
    ticket=index.reserve_probe(ProbeDraft(private_root_path=sys.argv[2],label='Synthetic crash'),request_id=str(uuid4()))
    request=index.begin_attempt(ticket)
    runtime=RuntimeIdentity.model_validate(WindowsAPI().runtime_identity())
    native=NativeOwner(); argv,env,cwd=_launch()
    if sys.argv[3]:
        argv=[sys.argv[3],'--workflow-nas-worker']; cwd=str(Path(sys.argv[3]).parent)
    native.start(argv,env,cwd)
    key={k:getattr(request,k) for k in ('scan_id','attempt_id','owner_generation')}
    native.write(encode_frame({**key,'type':'init'}))
    hello=json.loads(native.read_stdout(4096))
    native.capture_worker(hello['pid']); native.capture_members()
    def birth(member): return ProcessBirth(pid=member.pid,created_filetime_100ns=member.created,role=member.role)
    owner=ProcessOwner(runner_instance_id=str(uuid4()),job_name=native.job_name,
        launcher=birth(next(m for m in native.members if m.role=='launcher')),
        worker=birth(next(m for m in native.members if m.role=='worker')),
        members=tuple(birth(m) for m in native.members if m.role=='member'),runtime=runtime,
        observed_at=datetime.now(timezone.utc))
    index.authorize_go(request,owner)
    # Crash between durable GO authorization and its pipe delivery: no NAS I/O.
    print(json.dumps({'scan_id':request.scan_id,'host_id':request.host_id,'root_id':request.root_id}),flush=True)
    sys.stdin.buffer.read(1)
    os._exit(0)
'''


def failure(code):
    error = OSError("synthetic native failure")
    error.winerror = code
    return error


def pending():
    root, host, export = (str(uuid4()) for _ in range(3))
    request = ScanRequest(scan_id=str(uuid4()), attempt_id=str(uuid4()), root_id=root,
                          host_id=host, export_id=export, root_revision=1,
                          mapping_revision=1, owner_generation=1, kind="probe_root",
                          private_root_path="C:/synthetic-only", profile=ScanProfile())
    owner = ProcessOwner(runner_instance_id=str(uuid4()), job_name="Local\\FilmpawScan-" + str(uuid4()),
                         launcher=ProcessBirth(pid=101, created_filetime_100ns=1001, role="launcher"),
                         worker=ProcessBirth(pid=102, created_filetime_100ns=1002, role="worker"),
                         members=(ProcessBirth(pid=103, created_filetime_100ns=1003, role="member"),),
                         runtime=RuntimeIdentity(pid=100, created_filetime_100ns=1000,
                                                 session_id=1, machine_digest="a" * 64),
                         observed_at=datetime.now(timezone.utc))
    return PendingOwner(request=request, owner=owner, state="unknown_owner")


class Native:
    def __init__(self):
        self.births = {100: 1000, 101: 1001, 102: 1002, 103: 1003}
        self.signaled = set(self.births)
        self.missing = set()
        self.snapshot = set(self.births)
        self.denied = set()
        self.job_present = True
        self.job_active = 0
        self.job_members = []
        self.handles = set()
        self.close_failure = False
        self.machine = "a" * 64
        self.session = 1
        self.snapshot_failure = False

    def runtime_identity(self):
        return {"pid": 200, "created_filetime_100ns": 2000,
                "session_id": self.session, "machine_digest": self.machine}

    def open_process(self, pid):
        if pid in self.denied:
            raise failure(5)
        if pid in self.missing:
            raise failure(87)
        self.handles.add(pid)
        return pid

    def process_birth(self, handle):
        return handle, self.births[handle]

    def process_ids(self):
        if self.snapshot_failure:
            raise failure(5)
        return tuple(self.snapshot)

    def wait(self, handle, timeout):
        assert timeout == 0
        return handle in self.signaled

    def open_job(self, name):
        if not self.job_present:
            raise failure(2)
        self.handles.add(999)
        return 999

    def active(self, handle):
        return self.job_active

    def members(self, handle):
        return self.job_members

    def close(self, handle):
        if self.close_failure:
            raise failure(5)
        self.handles.remove(handle)


def test_complete_persisted_births_are_inspected_and_proof_is_exact_bound():
    value, api = pending(), Native()
    proof = RecoveryInspection(value, api=api).check(time.monotonic() + 1)
    assert proof is not None and proof.released
    assert proof.owner_digest == digest(value.owner)
    assert proof.request_digest == digest(value.request)
    assert proof.attempt_id == value.request.attempt_id
    assert proof.owner_generation == value.request.owner_generation
    assert proof.runtime.disposition == "handle_signaled"
    assert {p.disposition for p in proof.processes} == {"handle_signaled"}
    assert proof.job_state == "empty"
    assert not api.handles
    assert "io_joined" not in proof.model_dump()


def test_ordinary_crash_absence_is_separate_from_handle_signal():
    value, api = pending(), Native()
    api.missing = set(api.births)
    api.snapshot.clear()
    api.job_present = False
    proof = RecoveryInspection(value, api=api).check(time.monotonic() + 1)
    assert proof is not None and proof.released
    assert proof.runtime.disposition == "pid_absent"
    assert {p.disposition for p in proof.processes} == {"pid_absent"}
    assert proof.job_state == "absent"
    assert all(p.observed_created_filetime_100ns is None for p in proof.processes)
    assert not api.handles


@pytest.mark.parametrize("case", ["legacy", "no_owner", "uncertain", "unresolved", "machine", "session",
                                  "runtime_live", "child_live", "job_live", "extra_member", "denied",
                                  "false_absence", "snapshot_denied"])
def test_insufficient_or_contradictory_recovery_evidence_keeps_owner(case):
    value, api = pending(), Native()
    if case == "legacy":
        value = value.model_copy(update={"owner": value.owner.model_copy(update={"runtime": None})})
    elif case == "no_owner":
        value = value.model_copy(update={"owner": None})
    elif case == "uncertain":
        value = value.model_copy(update={"owner": value.owner.model_copy(update={"identity_uncertain": True})})
    elif case == "unresolved":
        value = value.model_copy(update={"owner": value.owner.model_copy(update={"unresolved_member_pids": (104,)})})
    elif case == "machine":
        api.machine = "b" * 64
    elif case == "session":
        api.session = 2
    elif case == "runtime_live":
        api.signaled.remove(100)
    elif case == "child_live":
        api.signaled.remove(103)
    elif case == "job_live":
        api.job_active = 1
    elif case == "extra_member":
        api.job_members = [9999]
    elif case == "denied":
        api.denied.add(103)
    elif case in {"false_absence", "snapshot_denied"}:
        api.missing.add(103)
        api.snapshot_failure = case == "snapshot_denied"
    assert RecoveryInspection(value, api=api).check(time.monotonic() + 1) is None
    assert not api.handles


def test_reused_pid_is_never_waited_as_old_birth_or_terminated():
    value, api = pending(), Native()
    api.births[103] = 3003
    api.signaled.remove(103)
    proof = RecoveryInspection(value, api=api).check(time.monotonic() + 1)
    assert proof is not None and proof.released
    reused = next(p for p in proof.processes if p.pid == 103)
    assert reused.disposition == "birth_reused"
    assert reused.observed_created_filetime_100ns == 3003
    assert not api.handles


def test_failed_close_retains_acquired_handles_until_a_proven_retry():
    value, api = pending(), Native()
    api.close_failure = True
    inspector = RecoveryInspection(value, api=api)
    assert inspector.check(time.monotonic() + 1) is None
    assert api.handles and not inspector.closed
    api.close_failure = False
    proof = inspector.check(time.monotonic() + 1)
    assert proof is not None and proof.released and inspector.closed
    assert not api.handles


def stored_owner(db, tmp_path):
    from filmpaw_server.workflow_index import WorkflowIndex
    index = WorkflowIndex(db, threading.RLock())
    ticket = index.reserve_probe(ProbeDraft(private_root_path=str(tmp_path), label="Synthetic"), request_id=str(uuid4()))
    request = index.begin_attempt(ticket)
    owner = pending().owner
    index.authorize_go(request, owner)
    restarted = WorkflowIndex(db, threading.RLock())
    record = restarted.pending_owners()[0]
    proof = RecoveryInspection(record, api=Native()).check(time.monotonic() + 1)
    return restarted, record, proof


def test_recovery_store_only_ends_exact_owner_without_publishing(db, tmp_path):
    index, record, proof = stored_owner(db, tmp_path)
    result = index.recover_owner(record.request, proof)
    assert result.owner_state == "released" and result.status == "cancelled" and not result.published
    assert not index.pending_owners()
    assert index.recover_owner(record.request, proof) == result
    row = db.execute("SELECT publication_allowed,outcome_json FROM workflow_scan_operations WHERE scan_id=?", (record.request.scan_id,)).fetchone()
    assert row["publication_allowed"] == 0
    from filmpaw_server.workflow_models import ScanOutcome
    outcome = ScanOutcome.model_validate_json(row["outcome_json"])
    assert outcome.snapshot is None and outcome.exit_proof is None and outcome.recovery_proof == proof
    assert db.execute("SELECT last_success_at FROM workflow_roots WHERE root_id=?", (record.request.root_id,)).fetchone()[0] is None


@pytest.mark.parametrize("field,value", [
    ("scan_id", str(uuid4())), ("attempt_id", str(uuid4())), ("owner_generation", 2),
    ("owner_digest", "b" * 64), ("request_digest", "b" * 64),
    ("job_name", "Local\\FilmpawScan-" + str(uuid4())), ("handles_closed", False),
])
def test_recovery_store_rejects_wrong_binding(db, tmp_path, field, value):
    index, record, proof = stored_owner(db, tmp_path)
    changed = proof.model_copy(update={field: value})
    with pytest.raises(WorkflowIndexError):
        index.recover_owner(record.request, changed)
    assert index.pending_owners() == (record,)


def test_recovery_store_requires_every_saved_birth(db, tmp_path):
    index, record, proof = stored_owner(db, tmp_path)
    with pytest.raises(WorkflowIndexError):
        index.recover_owner(record.request, proof.model_copy(update={"processes": proof.processes[:-1]}))
    assert index.pending_owners() == (record,)


def test_recovery_store_does_not_clear_sticky_or_new_identity(db, tmp_path):
    index, record, proof = stored_owner(db, tmp_path)
    index.update_owner(record.request, record.owner.model_copy(update={"identity_uncertain": True}))
    with pytest.raises(WorkflowIndexError):
        index.recover_owner(record.request, proof)
    assert index.pending_owners()[0].owner.identity_uncertain


def test_recovery_store_rejects_different_request_and_runtime(db, tmp_path):
    index, record, proof = stored_owner(db, tmp_path)
    changed_request = record.request.model_copy(update={"private_root_path": "C:/different-synthetic-root"})
    with pytest.raises(WorkflowIndexError, match="stale_binding"):
        index.recover_owner(changed_request, proof.model_copy(update={"request_digest": digest(changed_request)}))
    changed_runtime = proof.original_runtime.model_copy(update={"created_filetime_100ns": 5000})
    changed_proof = proof.model_copy(update={
        "original_runtime": changed_runtime,
        "runtime": proof.runtime.model_copy(update={
            "expected_created_filetime_100ns": 5000, "observed_created_filetime_100ns": 5000}),
    })
    with pytest.raises(WorkflowIndexError, match="unknown_owner"):
        index.recover_owner(record.request, changed_proof)
    assert index.pending_owners() == (record,)


def test_recovery_store_transaction_rolls_back_root_and_owner_together(db, tmp_path, monkeypatch):
    import sqlite3
    index, record, proof = stored_owner(db, tmp_path)

    def rows():
        return tuple(tuple(tuple(row) for row in db.execute("SELECT * FROM " + table))
                     for table in ("workflow_roots", "workflow_scan_operations", "workflow_index_meta"))

    before = rows()
    bump = index._bump

    def failed_write():
        bump()
        raise sqlite3.OperationalError("synthetic write failure after root and index mutation")

    monkeypatch.setattr(index, "_bump", failed_write)
    with pytest.raises(sqlite3.OperationalError):
        index.recover_owner(record.request, proof)
    assert rows() == before
    assert index.pending_owners() == (record,)
    monkeypatch.setattr(index, "_bump", bump)
    assert index.recover_owner(record.request, proof).owner_state == "released"


def test_recovery_store_repeated_and_late_finishes_cannot_publish(db, tmp_path):
    from tests.test_workflow_index_state import Harness
    index, record, proof = stored_owner(db, tmp_path)
    h = Harness(db)
    snapshot_outcome = h.outcome(record.request, record.owner)
    assert snapshot_outcome.snapshot.coverage == "complete"
    result = index.recover_owner(record.request, proof)
    before = tuple(db.execute("SELECT * FROM workflow_scan_operations").fetchone())
    assert index.recover_owner(record.request, proof) == result
    assert index.finish(record.request, snapshot_outcome) == result
    assert tuple(db.execute("SELECT * FROM workflow_scan_operations").fetchone()) == before
    assert db.execute("SELECT count(*) FROM sources").fetchone()[0] == 0
    assert db.execute("SELECT count(*) FROM workflow_locations").fetchone()[0] == 0


@pytest.mark.parametrize("error", ["permission_denied", "identity_changed", "identity_unknown", "not_directory"])
def test_recovery_restores_configuration_pause_hidden_by_unknown_owner(db, tmp_path, error):
    index, record, proof = stored_owner(db, tmp_path)
    failed = index.finish(record.request, ScanOutcome(status="failed", owner_state="unknown_owner", error_code=error))
    assert failed.error_code == "unknown_owner"
    assert index.recover_owner(record.request, proof).owner_state == "released"
    root = db.execute("SELECT configuration_reason,last_success_at,active FROM workflow_roots WHERE root_id=?",
                      (record.request.root_id,)).fetchone()
    assert tuple(root) == (error, None, 0)
    draft = ProbeDraft(private_root_path=record.request.private_root_path, label="Synthetic")
    with pytest.raises(WorkflowIndexError, match="configuration_required"):
        index.reserve_probe(draft, request_id=str(uuid4()))
    ticket = index.reserve_probe(draft, request_id=str(uuid4()), manual=True)
    assert index.begin_attempt(ticket).root_id == record.request.root_id
    assert db.execute("SELECT configuration_reason FROM workflow_roots WHERE root_id=?",
                      (record.request.root_id,)).fetchone()[0] == error


def test_startup_dispatch_recovers_before_admission(db, tmp_path, monkeypatch):
    import filmpaw_server.scan_process as scans
    index, record, proof = stored_owner(db, tmp_path)
    monkeypatch.setattr(scans, "RecoveryInspection", lambda value: RecoveryInspection(value, api=Native()))
    supervisor = scans.ScanSupervisor(index)
    checked = threading.Event()

    def tickets():
        assert not index.pending_owners()
        checked.set()
        return ()

    monkeypatch.setattr(index, "queued_tickets", tickets)
    supervisor.start()
    try:
        assert checked.wait(2)
        assert index.get_scan(record.request.scan_id).owner_state == "released"
    finally:
        assert supervisor.close().clean


def test_recovery_storage_failure_keeps_exit_blocked_and_retryable(db, tmp_path, monkeypatch):
    import sqlite3
    import filmpaw_server.scan_process as scans
    index, record, proof = stored_owner(db, tmp_path)
    monkeypatch.setattr(scans, "RecoveryInspection", lambda value: RecoveryInspection(value, api=Native()))
    supervisor = scans.ScanSupervisor(index)
    original = index.recover_owner

    def broken(*args):
        raise sqlite3.OperationalError("synthetic failed recovery commit")

    monkeypatch.setattr(index, "recover_owner", broken)
    with pytest.raises(WorkflowIndexError, match="scan_exit_pending"):
        supervisor.prepare_exit(lambda: pytest.fail("uncommitted proof cannot exit"))
    assert index.pending_owners() == (record,)
    assert not supervisor.close().clean
    monkeypatch.setattr(index, "recover_owner", original)
    assert supervisor.prepare_exit(lambda: {"safe": True})["safe"]
    assert supervisor.close().clean


@pytest.mark.skipif(sys.platform != "win32", reason="Windows Toolhelp snapshot")
def test_real_complete_toolhelp_snapshot_includes_self_and_closes_handle():
    from filmpaw_server.scan_native import WindowsAPI
    api = WindowsAPI()
    assert os.getpid() in api.process_ids()
    assert not api.snapshot_handles


@pytest.mark.skipif(sys.platform != "win32", reason="Windows Toolhelp snapshot")
def test_toolhelp_nonterminal_error_never_returns_partial_absence(monkeypatch):
    import ctypes
    from filmpaw_server.scan_native import WindowsAPI
    api = WindowsAPI()

    def denied(*args):
        ctypes.set_last_error(5)
        return 0

    monkeypatch.setattr(api.k, "Process32NextW", denied)
    with pytest.raises(OSError):
        api.process_ids()
    assert not api.snapshot_handles


@pytest.mark.skipif(sys.platform != "win32", reason="Windows Toolhelp snapshot")
def test_toolhelp_failed_close_retains_snapshot_without_proving_absence(monkeypatch):
    from filmpaw_server.scan_native import WindowsAPI
    api = WindowsAPI()
    close = api.close

    def denied(handle):
        raise failure(5)

    monkeypatch.setattr(api, "close", denied)
    try:
        with pytest.raises(OSError):
            api.process_ids()
        assert api.snapshot_handles
    finally:
        monkeypatch.setattr(api, "close", close)
        legacy = pending().model_copy(update={"owner": None})
        inspector = RecoveryInspection(legacy, api=api)
        assert inspector.check(time.monotonic() + 1) is None
        assert inspector.closed and not api.snapshot_handles


@pytest.mark.skipif(sys.platform != "win32", reason="Windows native crash recovery")
@pytest.mark.parametrize("frozen", [False, True])
def test_real_parent_crash_recovers_without_retained_child_handles(tmp_path, frozen):
    from filmpaw_server.db import connect
    from filmpaw_server.scan_process import ScanSupervisor
    from filmpaw_server.workflow_index import WorkflowIndex
    binary = os.environ.get("FILMPAW_FROZEN_BINARY", "") if frozen else ""
    if frozen and not binary:
        pytest.skip("explicit freshly-built FILMPAW_FROZEN_BINARY required")
    path = tmp_path / "crash.db"
    root = tmp_path / "synthetic-root"
    root.mkdir()
    parent = subprocess.Popen([sys.executable, "-c", CRASH_PARENT, str(path), str(root), binary],
                              stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              creationflags=0x08000000)
    db = None
    try:
        data = json.loads(parent.stdout.readline())
        parent.stdin.write(b"x")
        parent.stdin.flush()
        assert parent.wait(timeout=10) == 0
        # Close our Popen process HANDLE too: recovery must handle truly gone
        # births, not rely on diagnostic handles keeping old objects alive.
        parent._handle.Close()
        db = connect(path)
        index = WorkflowIndex(db, threading.RLock())
        saved = index.pending_owners()[0]
        assert saved.owner.runtime is not None
        assert not saved.owner.identity_uncertain
        supervisor = ScanSupervisor(index)
        end = time.monotonic() + 5
        while not supervisor.reconcile(data["host_id"]).clean and time.monotonic() < end:
            time.sleep(.02)
        assert not index.pending_owners()
        result = index.get_scan(data["scan_id"])
        assert result.status == "cancelled" and result.owner_state == "released" and not result.published
        row = db.execute("SELECT publication_allowed,outcome_json FROM workflow_scan_operations WHERE scan_id=?", (data["scan_id"],)).fetchone()
        assert row["publication_allowed"] == 0
        outcome = json.loads(row["outcome_json"])
        assert outcome["snapshot"] is None and outcome["exit_proof"] is None
        assert outcome["recovery_proof"]["job_state"] == "absent"
        assert supervisor.prepare_exit(lambda: {"safe": True})["safe"]
        assert supervisor.close().clean
    finally:
        if parent.poll() is None:
            parent.stdin.write(b"x")
            parent.stdin.flush()
            parent.wait(timeout=10)
        for stream in (parent.stdin, parent.stdout, parent.stderr):
            stream.close()
        if db is not None:
            db.close()
