"""Explicit packaging gate; run with FILMPAW_FROZEN_BINARY pointing to this build."""

import json
import os
from pathlib import Path
import queue
import sqlite3
import sys
import threading
import time
import urllib.request
from uuid import uuid4

import pytest

from filmpaw_server.scan_native import NativeOwner
from filmpaw_server.scan_protocol import encode_frame


@pytest.fixture
def binary():
    value = os.environ.get("FILMPAW_FROZEN_BINARY")
    if sys.platform != "win32" or not value:
        pytest.skip("explicit owned Windows frozen build required")
    result = Path(value).resolve()
    assert result.is_file()
    return result


def _lines(owner, output):
    buffer = b""
    while chunk := owner.read_stdout():
        buffer += chunk
        while b"\n" in buffer:
            line, buffer = buffer.split(b"\n", 1)
            output.put(line.strip())


def _discard(owner):
    while owner.read_stderr():
        pass


def test_frozen_worker_hello_before_go_has_no_database_or_service(binary, tmp_path):
    db = tmp_path / "must-not-open.sqlite"
    env = {**os.environ, "FILMPAW_DB": str(db), "FILMPAW_SESSION_TOKEN": "a" * 64}
    owner, output, threads = NativeOwner(), queue.Queue(), []
    try:
        owner.start([str(binary), "--workflow-nas-worker"], env, str(tmp_path))
        for target, args in ((_lines, (owner, output)), (_discard, (owner,))):
            thread = threading.Thread(target=target, args=args, daemon=True)
            threads.append(thread)
            thread.start()
        key = {"scan_id": str(uuid4()), "attempt_id": str(uuid4()), "owner_generation": 1}
        owner.write(encode_frame({**key, "type": "init"}))
        hello = json.loads(output.get(timeout=15))
        assert hello == {**key, "type": "hello", "schema_version": 1, "pid": hello["pid"]}
        owner.capture_worker(hello["pid"])
        owner.capture_members()
        assert not db.exists()
        assert output.empty()  # No FILMPAW_PORT/PID or service output.
        proof = owner.settle(time.monotonic() + 5, threads=threads)
        assert proof.released, proof.error_codes
        print("frozen worker birth roles:", [(m.role, m.pid, m.created, m.signaled) for m in owner.members])
    finally:
        assert owner.settle(time.monotonic() + 5, threads=threads).released


def test_frozen_service_same_exe_scans_and_prepares_exit(binary, tmp_path):
    db = tmp_path / "owned-library.sqlite"
    root = tmp_path / "owned-root"
    (root / "Synthetic Actor" / "Synthetic Film").mkdir(parents=True)
    env = {**os.environ, "FILMPAW_DB": str(db), "FILMPAW_SESSION_TOKEN": "b" * 64}
    owner, output, threads = NativeOwner(), queue.Queue(), []
    try:
        owner.start([str(binary)], env, str(tmp_path))
        for target, args in ((_lines, (owner, output)), (_discard, (owner,))):
            thread = threading.Thread(target=target, args=args, daemon=True)
            threads.append(thread)
            thread.start()
        port_line = output.get(timeout=15).decode()
        pid_line = output.get(timeout=5).decode()
        assert port_line.startswith("FILMPAW_PORT=") and pid_line.startswith("FILMPAW_PID=")
        owner.capture_worker(int(pid_line.split("=")[1]))
        port = int(port_line.split("=")[1])

        def call(path, body=None):
            request = urllib.request.Request(f"http://127.0.0.1:{port}{path}",
                data=None if body is None else json.dumps(body).encode(),
                headers={"X-Filmpaw-Session": "b" * 64, "Content-Type": "application/json"})
            with urllib.request.urlopen(request, timeout=20) as response:
                return json.load(response)

        deadline = time.monotonic() + 10
        while True:
            try:
                assert call("/api/health")["status"] == "ok"
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(.02)
        source = call("/api/sources", {"unc_path": str(root), "label": "Synthetic"})
        assert call(f"/api/sources/{source['id']}/scan", {})["added"] == 1
        assert call("/api/archive/prepare-exit", {}) == {"safe": True, "task_id": None}
        with sqlite3.connect(db) as conn:
            rows = conn.execute("SELECT owner_state,owner_json,outcome_json FROM workflow_scan_operations").fetchall()
        assert len(rows) == 2
        for state, observed, outcome in rows:
            assert state == "released"
            observed, outcome = json.loads(observed), json.loads(outcome)
            assert observed["worker"] is not None
            assert outcome["exit_proof"]["handles_closed"]
            assert all(item["signaled"] for item in outcome["exit_proof"]["processes"])
            print("frozen same-exe worker:", observed["launcher"], observed["worker"])
    finally:
        assert owner.settle(time.monotonic() + 5, threads=threads).released
