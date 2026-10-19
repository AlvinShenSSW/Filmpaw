import asyncio
import sys
import threading
import time
import os
from uuid import uuid4

import pytest

from filmpaw_server.scan_process import ScanSupervisor
from filmpaw_server.workflow_index import WorkflowIndex
from filmpaw_server.workflow_models import ProbeDraft, WorkflowIndexError, ScanProfile


@pytest.mark.skipif(sys.platform != "win32", reason="Windows scanner")
def test_source_worker_probe_and_scan_publish_only_after_release(db, tmp_path):
    root = tmp_path / "synthetic-root"
    (root / "Actor" / "Movie").mkdir(parents=True)
    index = WorkflowIndex(db, threading.RLock())
    supervisor = ScanSupervisor(index)
    supervisor.start()
    try:
        ticket = supervisor.request_probe(ProbeDraft(private_root_path=str(root), label="Synthetic"), request_id=str(uuid4()))
        result = asyncio.run(supervisor.wait(ticket, deadline=time.monotonic() + 15))
        assert result.published and result.owner_state == "released"
        ticket = supervisor.request(index.root_for_source(result.source_id), request_id=str(uuid4()))
        result = asyncio.run(supervisor.wait(ticket, deadline=time.monotonic() + 15))
        assert result.published and result.added == 1
        assert not index.pending_owners()
        assert supervisor.prepare_exit(lambda: {"safe": True, "task_id": None})["safe"]
        with pytest.raises(WorkflowIndexError, match="scan_exit_pending"):
            supervisor.request(ticket.root_id, request_id=str(uuid4()))
    finally:
        assert supervisor.close().clean


def test_transfer_busy_reopens_scanner_admission(db):
    index = WorkflowIndex(db, threading.RLock())
    supervisor = ScanSupervisor(index)
    supervisor.start()
    try:
        assert supervisor.prepare_exit(lambda: {"safe": False, "task_id": "synthetic"})["safe"] is False
        ticket = supervisor.request_probe(ProbeDraft(private_root_path="Z:/synthetic", label="Synthetic"), request_id=str(uuid4()))
        assert ticket.scan_id
    finally:
        supervisor.close()


def test_exit_fence_serializes_request_before_safe(db):
    index = WorkflowIndex(db, threading.RLock())
    supervisor = ScanSupervisor(index)
    entered, release = threading.Event(), threading.Event()
    outcomes = []

    def callback():
        entered.set()
        assert release.wait(5)
        return {"safe": True, "task_id": None}

    closing = threading.Thread(target=lambda: outcomes.append(supervisor.prepare_exit(callback)))
    closing.start()
    assert entered.wait(5)
    errors = []

    def request():
        try:
            supervisor.request_probe(ProbeDraft(private_root_path="Z:/synthetic", label="Synthetic"), request_id=str(uuid4()))
        except WorkflowIndexError as error:
            errors.append(error.code)

    waiting = threading.Thread(target=request)
    waiting.start()
    try:
        release.set()
        closing.join(5)
        waiting.join(5)
        assert errors == ["scan_exit_pending"]
        assert outcomes == [{"safe": True, "task_id": None}]
        assert supervisor.close().clean
    finally:
        release.set()
        closing.join(5)
        waiting.join(5)


def test_unknown_persisted_owner_blocks_exit_without_transfer_callback(db, tmp_path):
    index = WorkflowIndex(db, threading.RLock())
    ticket = index.reserve_probe(ProbeDraft(private_root_path=str(tmp_path), label="Synthetic"), request_id=str(uuid4()))
    index.begin_attempt(ticket)
    restarted = WorkflowIndex(db, threading.RLock())
    supervisor = ScanSupervisor(restarted)
    with pytest.raises(WorkflowIndexError, match="scan_exit_pending"):
        supervisor.prepare_exit(lambda: pytest.fail("unknown scanner must fence transfer close"))
    assert not supervisor.close().clean
    assert restarted.pending_owners()[0].state == "unknown_owner"


def test_repeated_exit_storage_failure_keeps_prior_safe_admission_closed(db, tmp_path, monkeypatch):
    import sqlite3
    from filmpaw_server.transfer import TransferService

    lock = threading.RLock()
    index = WorkflowIndex(db, lock)
    supervisor = ScanSupervisor(index)
    transfers = TransferService(db, lock)
    ticket = index.reserve_probe(ProbeDraft(private_root_path=str(tmp_path), label="Synthetic"), request_id=str(uuid4()))
    try:
        assert supervisor.prepare_exit(transfers.prepare_exit)["safe"]

        def storage_failure():
            raise sqlite3.OperationalError("synthetic storage failure")

        monkeypatch.setattr(transfers, "_busy_locked", storage_failure)
        with pytest.raises(WorkflowIndexError, match="scan_exit_pending"):
            supervisor.prepare_exit(transfers.prepare_exit)
        assert supervisor._closing
        with pytest.raises(WorkflowIndexError, match="scan_exit_pending"):
            supervisor.request(ticket.root_id, request_id=str(uuid4()))
        supervisor.start()
        assert supervisor._thread is None
        assert index.get_scan(ticket.scan_id).status == "queued"
    finally:
        transfers.close()
        assert supervisor.close().clean


@pytest.mark.skipif(sys.platform != "win32", reason="Windows scanner")
def test_manual_scan_recovers_open_circuit_without_auto_refresh(db, tmp_path):
    from datetime import timedelta
    from tests.test_workflow_index_state import Harness

    root = tmp_path / "owned-root"
    (root / "Synthetic Actor").mkdir(parents=True)
    h = Harness(db)
    setup = ScanSupervisor(h.index)
    setup.start()
    try:
        probe = setup.request_probe(ProbeDraft(private_root_path=str(root), label="Synthetic"), request_id=str(uuid4()))
        assert asyncio.run(setup.wait(probe, deadline=time.monotonic() + 10)).published
    finally:
        assert setup.close().clean

    for delay in (31, 121, 901):
        req, owner = h.start(h.index.reserve_scan(probe.root_id, request_id=str(uuid4())))
        failed = h.outcome(req, owner, status="failed").model_copy(update={"snapshot": None, "error_code": "offline"})
        assert not h.index.finish(req, failed).published
        h.now += timedelta(seconds=delay)
    supervisor = ScanSupervisor(h.index)
    supervisor.start(auto_refresh=False)
    try:
        full = supervisor.request(probe.root_id, request_id=str(uuid4()))
        result = asyncio.run(supervisor.wait(full, deadline=time.monotonic() + 5))
        assert result.published and result.added == 1
        assert h.index.get_scan(full.scan_id).published
        with h.lock:
            rows = h.db.execute("SELECT kind,state,owner_state FROM workflow_scan_operations WHERE root_id=?", (full.root_id,)).fetchall()
            assert sum(row["kind"] == "probe_root" and row["state"] == "complete" for row in rows) == 2
            assert all(row["owner_state"] == "released" for row in rows)
    finally:
        assert supervisor.close().clean


@pytest.mark.skipif(sys.platform != "win32", reason="Windows scanner")
def test_missing_root_releases_actual_process_and_records_failure(db, tmp_path):
    index = WorkflowIndex(db, threading.RLock())
    supervisor = ScanSupervisor(index)
    supervisor.start()
    try:
        ticket = supervisor.request_probe(ProbeDraft(private_root_path=str(tmp_path / "missing"), label="Synthetic"), request_id=str(uuid4()))
        result = asyncio.run(supervisor.wait(ticket, deadline=time.monotonic() + 15))
        assert not result.published
        assert result.owner_state == "released"
        assert result.error_code in {"offline", "not_directory"}
        assert not index.pending_owners()
    finally:
        assert supervisor.close().clean


@pytest.mark.skipif(sys.platform != "win32", reason="Windows scanner")
@pytest.mark.parametrize("mode", ["wrong_generation", "boolean_generation", "wrong_pid", "short_eof", "no_complete"])
def test_invalid_worker_protocol_never_publishes_and_releases_known_process(db, tmp_path, monkeypatch, mode):
    from filmpaw_server import scan_process
    script = tmp_path / "synthetic_worker.py"
    script.write_text('''import json,os,sys
init=json.loads(sys.stdin.buffer.readline())
mode=sys.argv[1]
hello={k:v for k,v in init.items() if k!="type"}
hello.update(type="hello",pid=os.getpid(),schema_version=1)
if mode=="wrong_generation": hello["owner_generation"]+=1
if mode=="boolean_generation": hello["owner_generation"]=True
if mode=="wrong_pid": hello["pid"]=int(sys.argv[3])
print(json.dumps(hello),flush=True)
if mode in ("wrong_generation","boolean_generation","wrong_pid"):
    if sys.stdin.buffer.readline():
        open(sys.argv[2],"w").write("GO should not arrive")
elif mode=="short_eof":
    sys.stdout.write("{"); sys.stdout.flush()
''', encoding="utf-8")
    marker = tmp_path / "go-marker"
    monkeypatch.setattr(scan_process, "_launch", lambda: ([sys.executable, str(script), mode, str(marker), str(os.getpid())], dict(os.environ), str(tmp_path)))
    index = WorkflowIndex(db, threading.RLock())
    supervisor = ScanSupervisor(index)
    supervisor.start()
    try:
        ticket = supervisor.request_probe(ProbeDraft(private_root_path=str(tmp_path), label="Synthetic"), request_id=str(uuid4()))
        result = asyncio.run(supervisor.wait(ticket, deadline=time.monotonic() + 15))
        assert not result.published and result.status == "failed"
        assert not marker.exists()
        # A hello pointing outside this Job is identity uncertainty, never an adopted PID.
        if mode != "wrong_pid":
            assert result.owner_state == "released"
    finally:
        cleaned = supervisor.close().clean
        assert cleaned or mode == "wrong_pid"
        if mode == "wrong_pid":
            # The deliberately untrusted external PID belongs to this test.
            # Release test handles only; production correctly retains uncertainty.
            for attempt in supervisor._attempts.values():
                assert not attempt.thread.is_alive()
                for handle in tuple(attempt.native.handles):
                    attempt.native._close(handle)
                if attempt.native.job is not None:
                    attempt.native.api.close(attempt.native.job)
                    attempt.native.job = None


@pytest.mark.skipif(sys.platform != "win32", reason="Windows scanner")
def test_launch_environment_does_not_inherit_tokens_or_database(monkeypatch):
    from filmpaw_server.scan_process import _launch
    monkeypatch.setenv("FILMPAW_DB", "private-library")
    monkeypatch.setenv("FILMPAW_SESSION_TOKEN", "secret")
    monkeypatch.setenv("ARBITRARY_API_TOKEN", "secret")
    monkeypatch.setenv("_PYI_PARENT_PROCESS_LEVEL", "1")
    argv, env, _ = _launch()
    assert argv[-1] == "--workflow-nas-worker"
    assert not any(key in env for key in ("FILMPAW_DB", "FILMPAW_SESSION_TOKEN", "ARBITRARY_API_TOKEN"))
    assert env["_PYI_PARENT_PROCESS_LEVEL"] == "1"


@pytest.mark.skipif(sys.platform != "win32", reason="Windows scanner")
def test_persistence_failure_retains_owner_and_exit_retries_same_result(db, tmp_path, monkeypatch):
    index = WorkflowIndex(db, threading.RLock())
    original = index.finish
    held, reached = threading.Event(), threading.Event()
    held.set()
    def failing(request, outcome):
        reached.set()
        if held.is_set():
            raise OSError("synthetic transaction failure")
        return original(request, outcome)
    monkeypatch.setattr(index, "finish", failing)
    supervisor = ScanSupervisor(index)
    supervisor.start()
    try:
        ticket = supervisor.request_probe(ProbeDraft(private_root_path=str(tmp_path), label="Synthetic"), request_id=str(uuid4()))
        assert reached.wait(10)
        attempt = supervisor._attempts[ticket.scan_id]
        attempt.thread.join(5)
        assert not attempt.thread.is_alive()
        assert attempt.native.exit_proven
        with pytest.raises(WorkflowIndexError, match="scan_exit_pending"):
            supervisor.prepare_exit(lambda: pytest.fail("persistence failure must retain exit fence"))
        assert index.pending_owners()
        held.clear()
        assert supervisor.prepare_exit(lambda: {"safe": True, "task_id": None})["safe"]
        assert index.get_scan(ticket.scan_id).owner_state == "released"
    finally:
        held.clear()
        assert supervisor.close().clean


@pytest.mark.skipif(sys.platform != "win32", reason="Windows scanner")
def test_timeout_terminates_blocked_worker_before_releasing_host(db, tmp_path, monkeypatch):
    from filmpaw_server import scan_process
    script = tmp_path / "blocked_worker.py"
    script.write_text('''import json,os,sys
init=json.loads(sys.stdin.buffer.readline())
init.update(type="hello",pid=os.getpid(),schema_version=1)
print(json.dumps(init),flush=True)
go=json.loads(sys.stdin.buffer.readline()); assert go["type"]=="go"
open(sys.argv[1],"w").write("entered")
sys.stdin.buffer.read()
''', encoding="utf-8")
    marker = tmp_path / "entered"
    monkeypatch.setattr(scan_process, "_launch", lambda: ([sys.executable, str(script), str(marker)], dict(os.environ), str(tmp_path)))
    index = WorkflowIndex(db, threading.RLock(), ScanProfile(probe_seconds=2.0))
    supervisor = ScanSupervisor(index)
    supervisor.start()
    try:
        ticket = supervisor.request_probe(ProbeDraft(private_root_path=str(tmp_path), label="Synthetic"), request_id=str(uuid4()))
        result = asyncio.run(supervisor.wait(ticket, deadline=time.monotonic() + 10))
        assert marker.exists()
        assert result.error_code == "timeout" and result.owner_state == "released"
        assert not result.published and not index.pending_owners()
    finally:
        assert supervisor.close().clean


@pytest.mark.skipif(sys.platform != "win32", reason="Windows scanner")
def test_native_createprocess_false_can_release_without_fabricated_process(db, tmp_path, monkeypatch):
    from filmpaw_server import scan_process
    monkeypatch.setattr(scan_process, "_launch", lambda: ([str(tmp_path / "not-present.exe")], {}, str(tmp_path)))
    index = WorkflowIndex(db, threading.RLock())
    supervisor = ScanSupervisor(index)
    supervisor.start()
    try:
        ticket = supervisor.request_probe(ProbeDraft(private_root_path=str(tmp_path), label="Synthetic"), request_id=str(uuid4()))
        result = asyncio.run(supervisor.wait(ticket, deadline=time.monotonic() + 10))
        assert result.error_code == "spawn_failed" and result.owner_state == "released"
        assert not index.pending_owners()
    finally:
        assert supervisor.close().clean
