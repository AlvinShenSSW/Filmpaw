"""Disposable local/UNC/mapped-drive cleanup checks; never uses a real library.

Run: uv run --project app/server --locked python docs/evidence/issue-38/cleanup-smoke.py
Uses an existing localhost administrative share only, with no share/ACL changes.
An optional free drive letter is mapped temporarily and removed after its check.
"""

import ctypes
import hashlib
import json
import os
import secrets
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi.testclient import TestClient

from filmpaw_server.app import create_app


def snapshot(root):
    return {
        path.relative_to(root).as_posix(): {
            "directory": path.is_dir(),
            "mtime_ns": path.stat().st_mtime_ns,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None,
        }
        for path in [root, *sorted(root.rglob("*"))]
    }


def connection_target(drive):
    from ctypes import wintypes
    mpr = ctypes.WinDLL("mpr", use_last_error=True)
    get = mpr.WNetGetConnectionW
    get.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
    get.restype = wintypes.DWORD
    size = wintypes.DWORD(32768)
    buffer = ctypes.create_unicode_buffer(size.value)
    result = get(drive, buffer, ctypes.byref(size))
    if result == 2250:  # ERROR_NOT_CONNECTED
        return None
    if result:
        raise OSError(f"Cannot verify mapping {drive}: WinError {result}")
    return buffer.value


def record_mapping(ledger, drive, remote):
    root = ledger.parent
    unc_root = Path(r"\\localhost\C$").joinpath(*root.parts[1:])
    assert os.path.commonpath([os.path.normcase(str(remote)), os.path.normcase(str(unc_root))]) == os.path.normcase(str(unc_root))
    records = json.loads(ledger.read_text()) if ledger.exists() else []
    records.append({"drive": drive, "remote": str(remote)})
    pending = ledger.with_suffix(".new")
    pending.write_text(json.dumps(records), encoding="utf-8")
    pending.replace(ledger)  # Persist ownership before an OS mapping mutation.


def cancel_owned_mapping(drive, allowed):
    from ctypes import wintypes
    current = connection_target(drive)
    if current is None:
        return
    assert os.path.normcase(current) in {os.path.normcase(remote) for remote in allowed}, "Mapping ownership changed; refusing removal"
    mpr = ctypes.WinDLL("mpr", use_last_error=True)
    cancel = mpr.WNetCancelConnection2W
    cancel.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.BOOL]
    cancel.restype = wintypes.DWORD
    result = cancel(drive, 0, False)
    if result:
        raise OSError(f"Could not remove our temporary drive {drive}: WinError {result}")


@contextmanager
def temporary_mapping(remote, ledger):
    from ctypes import wintypes

    class NetResource(ctypes.Structure):
        _fields_ = [
            ("scope", wintypes.DWORD), ("kind", wintypes.DWORD),
            ("display", wintypes.DWORD), ("usage", wintypes.DWORD),
            ("local", wintypes.LPWSTR), ("remote", wintypes.LPWSTR),
            ("comment", wintypes.LPWSTR), ("provider", wintypes.LPWSTR),
        ]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetLogicalDrives.restype = wintypes.DWORD
    used = kernel.GetLogicalDrives()
    drive = None
    for code in range(ord("Z"), ord("P"), -1):
        candidate = f"{chr(code)}:"
        if used & (1 << (code - ord("A"))):
            continue
        try:
            if connection_target(candidate) is None:
                drive = candidate
                break
        except OSError:
            continue  # A remembered/disconnected mapping is not free.
    if drive is None:
        yield None, "No unused drive letter available", None
        return
    mpr = ctypes.WinDLL("mpr", use_last_error=True)
    add = mpr.WNetAddConnection2W
    add.argtypes = [ctypes.POINTER(NetResource), wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD]
    add.restype = wintypes.DWORD
    resource = NetResource(kind=1, local=drive, remote=str(remote))
    record_mapping(ledger, drive, remote)
    result = add(ctypes.byref(resource), None, None, 4)  # CONNECT_TEMPORARY; no profile update
    if result:
        yield None, f"Temporary mapping unavailable: WinError {result}", None
        return
    allowed = [str(remote)]
    def remap(new_remote):
        record_mapping(ledger, drive, new_remote)
        cancel_owned_mapping(drive, allowed)
        resource.remote = str(new_remote)
        result = add(ctypes.byref(resource), None, None, 4)
        if result:
            raise OSError(f"Could not remap our temporary drive: WinError {result}")
        allowed.append(str(new_remote))
        assert os.path.normcase(connection_target(drive)) == os.path.normcase(str(new_remote))
    try:
        yield Path(drive + "\\"), None, remap
    finally:
        cancel_owned_mapping(drive, allowed)


def run_case(client, root, batch_path, label, after_context=None):
    local_batch = root / label
    local_batch.mkdir()
    (local_batch / "Empty Actor 中文, 1").mkdir()
    (local_batch / "Empty Actor 2").mkdir()
    (local_batch / "Only Poster").mkdir()
    (local_batch / "Only Poster" / "folder.jpg").write_bytes(b"disposable poster")
    (local_batch / "Only Hidden").mkdir()
    (local_batch / "Only Hidden" / ".keep").write_bytes(b"")
    (local_batch / "Only Subdirectory" / "empty-child").mkdir(parents=True)
    ads = local_batch / "Only Directory ADS"
    ads.mkdir()
    Path(str(ads) + ":fixture-note").write_bytes(b"named stream must survive")
    # Existing NAS/other-batch paths are deliberate same-name controls.
    outside = root / (label + "-outside")
    (outside / "Empty Actor 中文, 1").mkdir(parents=True)
    (outside / "Film" / "nested").mkdir(parents=True)
    (outside / "Film" / "video.mp4").write_bytes(b"outside selected batch")
    before = snapshot(outside)
    saved = client.put("/api/settings", json={"last_local_dir": str(batch_path)})
    saved.raise_for_status()
    approved = saved.json()["last_local_dir"]
    response = client.post("/api/archive/cleanup-contexts", json={"batch_path": approved})
    response.raise_for_status()
    context = response.json()
    binding = client.app.state.cleanups._contexts[context["id"]]["_binding"]
    if after_context:
        after_context()
    payload = {"context_id": context["id"], "request_id": f"fixture-{label}"}
    started = time.monotonic()
    response = client.post("/api/archive/cleanups", json=payload)
    response.raise_for_status()
    task_id = response.json()["id"]
    deadline = time.monotonic() + 40
    samples = []
    while True:
        before_query = time.perf_counter()
        result = client.get(f"/api/archive/cleanups/{task_id}")
        samples.append(round((time.perf_counter() - before_query) * 1000, 3))
        result.raise_for_status()
        task = result.json()
        if task["status"] not in {"queued", "running"}:
            break
        assert time.monotonic() < deadline, task
        time.sleep(0.02)
    refused = bool(after_context and task["status"] == "failed" and task["deleted_count"] == 0)
    assert refused or task["status"] == "completed", task
    if not refused:
        assert task["deleted_count"] == 2, task
        assert task["kept_count"] == 4 and task["failed_count"] == 0, task
    assert local_batch.is_dir()
    assert (local_batch / "Empty Actor 中文, 1").exists() == refused
    assert (local_batch / "Empty Actor 2").exists() == refused
    assert (local_batch / "Only Poster" / "folder.jpg").read_bytes() == b"disposable poster"
    assert Path(str(ads) + ":fixture-note").read_bytes() == b"named stream must survive"
    assert snapshot(outside) == before
    # A lost response retried with the same identity must not start another scan.
    (local_batch / "Empty Actor 2").mkdir(exist_ok=refused)
    replay = client.post("/api/archive/cleanups", json=payload)
    replay.raise_for_status()
    assert replay.json()["id"] == task_id
    assert (local_batch / "Empty Actor 2").is_dir()
    return {
        "location": label, "input_batch": str(batch_path), "approved_batch": approved,
        "native_root": binding, "status": task["status"], "safe_refusal": refused,
        "seconds": round(time.monotonic() - started, 3),
        "deleted": task["deleted_count"], "kept": task["kept_count"],
        "outside_tree_unchanged": True, "root_retained": True,
        "directory_ads_retained": True, "replay_did_not_delete_recreated_actor": True,
        "max_status_asgi_ms": max(samples), "items": task["items"],
    }


def worker(root):
    assert os.name == "nt", "This checks the Windows product's native deletion path"
    report = {"executed_utc": datetime.now(timezone.utc).isoformat(), "cases": []}
    assert root.parent == Path(tempfile.gettempdir()).resolve()
    assert root.name.startswith("filmpaw-38-cleanup-")
    database = root / "isolated.db"
    os.environ["FILMPAW_DB"] = str(database)
    token = secrets.token_hex(32)
    os.environ["FILMPAW_SESSION_TOKEN"] = token
    try:
        with TestClient(create_app(database), headers={"X-Filmpaw-Session": token}) as client:
            report["cases"].append(run_case(client, root, root / "local", "local"))
            assert root.drive.upper() == "C:", "Loopback fixture assumes existing C$"
            unc_root = Path(r"\\localhost\C$").joinpath(*root.parts[1:])
            assert unc_root.is_dir(), "Existing localhost C$ is unavailable; no share will be created"
            report["cases"].append(run_case(client, root, unc_root / "unc", "unc"))
            ledger = root / "mapping-ownership.json"
            with temporary_mapping(unc_root, ledger) as (mapped, reason, _):
                if mapped is None:
                    report["mapped_drive_skipped"] = reason
                else:
                    report["cases"].append(run_case(client, root, mapped / "mapped", "mapped"))
            original, replacement = root / "original", root / "replacement"
            original.mkdir()
            replacement.mkdir()
            control = replacement / "mapped-switch"
            (control / "Empty Actor 中文, 1").mkdir(parents=True)
            (control / "Empty Actor 2").mkdir()
            (control / "Film").mkdir()
            (control / "Film" / "marker.bin").write_bytes(b"replacement mapping must remain untouched")
            before = snapshot(replacement)
            with temporary_mapping(unc_root / "original", ledger) as (mapped, reason, remap):
                if mapped is None:
                    report["mapping_switch_skipped"] = reason
                else:
                    def switch():
                        remap(unc_root / "replacement")
                        assert (mapped / "mapped-switch" / "Film" / "marker.bin").read_bytes() == b"replacement mapping must remain untouched"
                    result = run_case(client, original, mapped / "mapped-switch", "mapped-switch", switch)
                    assert snapshot(replacement) == before
                    result["alias_replaced_after_context_before_worker"] = True
                    result["replacement_tree_unchanged"] = True
                    report["cases"].append(result)
        report["status"] = "passed"
    finally:
        (root / "worker-report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")


def main():
    assert os.name == "nt"
    report = {"executed_utc": datetime.now(timezone.utc).isoformat(), "cases": []}
    temporary = tempfile.TemporaryDirectory(prefix="filmpaw-38-cleanup-")
    root = Path(temporary.name).resolve()
    assert root.parent == Path(tempfile.gettempdir()).resolve()
    assert root.name.startswith("filmpaw-38-cleanup-")
    environment = os.environ.copy()
    environment["FILMPAW_DB"] = str(root / "isolated.db")
    cleanup_errors = []
    try:
        try:
            completed = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--worker", str(root)],
                                       stdin=subprocess.DEVNULL, capture_output=True, text=True,
                                       encoding="utf-8", errors="replace", env=environment, timeout=180)
            child_report = root / "worker-report.json"
            if child_report.exists():
                report.update(json.loads(child_report.read_text(encoding="utf-8")))
            report["worker_exit_code"] = completed.returncode
            if completed.returncode:
                report["status"] = "failed"
                report["failure"] = completed.stderr[-6000:]
        except subprocess.TimeoutExpired:
            report["status"] = "hard_timeout"
        report["stdin_closed"] = True
        report["hard_timeout_seconds"] = 180
    finally:
        ledger = root / "mapping-ownership.json"
        if ledger.exists():
            records = json.loads(ledger.read_text(encoding="utf-8"))
            for drive in {record["drive"] for record in records}:
                try:
                    cancel_owned_mapping(drive, [record["remote"] for record in records if record["drive"] == drive])
                except Exception as exc:
                    cleanup_errors.append(str(exc))
        report["temporary_drive_released"] = not cleanup_errors
        try:
            assert root.resolve().parent == Path(tempfile.gettempdir()).resolve()
            assert root.name.startswith("filmpaw-38-cleanup-")
            temporary.cleanup()
        except Exception as exc:
            cleanup_errors.append(str(exc))
            report["residual_directory"] = str(root)
        report["temporary_fixture_removed"] = not root.exists()
        if cleanup_errors:
            report["cleanup_errors"] = cleanup_errors
            report["status"] = "failed"
    output = Path(__file__).with_suffix(".json")
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"report": str(output), "cases": len(report["cases"]),
                      "status": report["status"], "temporary_fixture_removed": report["temporary_fixture_removed"]}))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--worker":
        worker(Path(sys.argv[2]).resolve())
    else:
        raise SystemExit(main())
