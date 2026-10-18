"""Reproduce the issue #36 frozen-sidecar checks on Windows.

Build first: uv run --project app/server --locked python app/server/build_sidecar.py
Run: python docs/evidence/issue-36/frozen-sidecar-smoke.py

All workers use fresh temporary databases and random in-memory credentials.
The supervising process applies a 180-second hard timeout and a Windows job
that contains the runner and all descendants, including orphaned onefile workers.
Only the adjacent JSON report is retained; verbose logs stay under .git/afk-36.
"""

from pathlib import Path
import argparse
from contextlib import contextmanager
import ctypes
from ctypes import wintypes
from datetime import datetime, timezone
import hashlib
import json
import os
import queue
import secrets
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request


ROOT = Path(__file__).resolve().parents[3]
OUT = ROOT / ".git" / "afk-36"
EXE = ROOT / "app/src-tauri/binaries/filmpaw-server-x86_64-pc-windows-msvc.exe"
EXPECTED = json.loads((ROOT / "app/src-tauri/tauri.conf.json").read_text())["version"]
KERNEL = ctypes.WinDLL("kernel32", use_last_error=True)
KERNEL.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
KERNEL.OpenProcess.restype = wintypes.HANDLE
KERNEL.GetProcessId.argtypes = [wintypes.HANDLE]
KERNEL.GetProcessId.restype = wintypes.DWORD
KERNEL.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
KERNEL.WaitForSingleObject.restype = wintypes.DWORD
KERNEL.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
KERNEL.TerminateProcess.restype = wintypes.BOOL
KERNEL.CloseHandle.argtypes = [wintypes.HANDLE]
KERNEL.CloseHandle.restype = wintypes.BOOL
KERNEL.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
KERNEL.QueryFullProcessImageNameW.restype = wintypes.BOOL


class BasicLimits(ctypes.Structure):
    _fields_ = [("process_time", ctypes.c_int64), ("job_time", ctypes.c_int64),
        ("flags", wintypes.DWORD), ("min_working_set", ctypes.c_size_t),
        ("max_working_set", ctypes.c_size_t), ("active_processes", wintypes.DWORD),
        ("affinity", ctypes.c_size_t), ("priority", wintypes.DWORD), ("scheduling", wintypes.DWORD)]


class IoCounters(ctypes.Structure):
    _fields_ = [(name, ctypes.c_uint64) for name in
        ("read_ops", "write_ops", "other_ops", "read_bytes", "write_bytes", "other_bytes")]


class ExtendedLimits(ctypes.Structure):
    _fields_ = [("basic", BasicLimits), ("io", IoCounters),
        ("process_memory", ctypes.c_size_t), ("job_memory", ctypes.c_size_t),
        ("peak_process_memory", ctypes.c_size_t), ("peak_job_memory", ctypes.c_size_t)]


class JobAccounting(ctypes.Structure):
    _fields_ = [("user_time", ctypes.c_int64), ("kernel_time", ctypes.c_int64),
        ("period_user_time", ctypes.c_int64), ("period_kernel_time", ctypes.c_int64),
        ("page_faults", wintypes.DWORD), ("total_processes", wintypes.DWORD),
        ("active_processes", wintypes.DWORD), ("terminated_processes", wintypes.DWORD)]


KERNEL.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
KERNEL.CreateJobObjectW.restype = wintypes.HANDLE
KERNEL.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
KERNEL.SetInformationJobObject.restype = wintypes.BOOL
KERNEL.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
KERNEL.AssignProcessToJobObject.restype = wintypes.BOOL
KERNEL.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
KERNEL.TerminateJobObject.restype = wintypes.BOOL
KERNEL.QueryInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
    wintypes.DWORD, ctypes.c_void_p]
KERNEL.QueryInformationJobObject.restype = wintypes.BOOL


def stop_job(job):
    assert KERNEL.TerminateJobObject(job, 1), ctypes.get_last_error()
    deadline = time.monotonic() + 15
    while True:
        accounting = JobAccounting()
        assert KERNEL.QueryInformationJobObject(job, 1, ctypes.byref(accounting),
            ctypes.sizeof(accounting), None), ctypes.get_last_error()
        if accounting.active_processes == 0:
            return
        if time.monotonic() >= deadline:
            raise TimeoutError("fixture processes did not leave their Windows job")
        time.sleep(0.05)


@contextmanager
def fixture_directory():
    temporary = tempfile.TemporaryDirectory(prefix="filmpaw-frozen-evidence-")
    fixture_root = Path(temporary.name).resolve()
    assert fixture_root.parent == Path(tempfile.gettempdir()).resolve()
    assert fixture_root.name.startswith("filmpaw-frozen-evidence-")
    try:
        yield fixture_root
    finally:
        # Job termination is asynchronous; even after all processes disappear,
        # Windows/AV can briefly retain a mapped PyInstaller DLL. Retry only
        # this verified fixture root, with a finite cleanup grace period.
        deadline = time.monotonic() + 10
        while True:
            try:
                temporary.cleanup()
                break
            except PermissionError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.1)


def scenario(configured: bool, fixture_root: Path) -> dict:
    result = {"configured_token": configured}
    with tempfile.TemporaryDirectory(prefix="scenario-", dir=fixture_root) as tmp:
        temp = Path(tmp).resolve()
        assert temp.is_relative_to(fixture_root.resolve())
        runtime = temp / "runtime"
        runtime.mkdir()
        token = secrets.token_hex(32)
        db = temp / "smoke.db"
        env = dict(os.environ, FILMPAW_DB=str(db), FILMPAW_DEV="0", TEMP=str(runtime), TMP=str(runtime))
        if configured:
            env["FILMPAW_SESSION_TOKEN"] = token
        else:
            env.pop("FILMPAW_SESSION_TOKEN", None)
        log_path = OUT / f"frozen-sidecar-{'authenticated' if configured else 'unconfigured'}.log"
        worker = None
        proc = None
        pump = None
        with log_path.open("w", encoding="utf-8") as log:
            try:
                proc = subprocess.Popen([str(EXE)], cwd=ROOT / "app/server", env=env,
                    stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=log,
                    text=True, encoding="utf-8", errors="replace", creationflags=subprocess.CREATE_NO_WINDOW)
                lines = queue.Queue()
                def drain():
                    for line in proc.stdout:
                        lines.put(line.strip())
                pump = threading.Thread(target=drain, daemon=True)
                pump.start()
                first = lines.get(timeout=30)
                second = lines.get(timeout=30)
                assert first.startswith("FILMPAW_PORT="), first
                assert second.startswith("FILMPAW_PID="), second
                port = int(first.split("=", 1)[1])
                worker_pid = int(second.split("=", 1)[1])
                assert worker_pid != proc.pid
                worker = KERNEL.OpenProcess(0x00100000 | 0x1000 | 0x0001, False, worker_pid)
                assert worker, ctypes.get_last_error()
                assert KERNEL.GetProcessId(worker) == worker_pid
                assert KERNEL.WaitForSingleObject(worker, 0) == 258
                image = ctypes.create_unicode_buffer(32768)
                image_len = wintypes.DWORD(len(image))
                assert KERNEL.QueryFullProcessImageNameW(worker, 0, image, ctypes.byref(image_len))
                assert Path(image.value).resolve() == EXE.resolve()
                result.update(bootloader_pid=proc.pid, worker_pid=worker_pid, worker_image_matches=True,
                    first_line_contract=True, worker_pid_distinct=True)

                def request(path, method="GET", payload=None, supplied=None, origin=None):
                    headers = {}
                    if supplied is not None:
                        headers["X-Filmpaw-Session"] = supplied
                    if origin is not None:
                        headers["Origin"] = origin
                    body = None if payload is None else json.dumps(payload).encode()
                    if body is not None:
                        headers["Content-Type"] = "application/json"
                    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=body, headers=headers, method=method)
                    try:
                        with urllib.request.urlopen(req, timeout=3) as response:
                            return response.status, json.loads(response.read())
                    except urllib.error.HTTPError as error:
                        return error.code, json.loads(error.read())

                deadline = time.monotonic() + 30
                while True:
                    try:
                        code, health = request("/api/health")
                        if code == 200:
                            break
                    except (OSError, urllib.error.URLError):
                        pass
                    assert time.monotonic() < deadline, "health readiness deadline"
                    time.sleep(0.1)
                assert health["version"] == EXPECTED == "0.5.0", health
                result["version"] = health["version"]
                assert request("/api/settings")[1]["db_path"] == str(db)
                result["temporary_database_verified"] = True
                assert request("/api/archive/transfers/latest")[0] == 403
                assert request("/api/archive/transfers/latest", supplied="0" * 64)[0] == 403
                assert request("/api/settings", "PUT", {}, supplied=None)[0] == 403
                assert request("/api/archive/prepare-exit", "POST", {}, supplied=None)[0] == 403
                result["missing_and_wrong_credentials_rejected"] = True
                if configured:
                    assert request("/api/archive/transfers/latest", supplied=token) == (200, None)
                    assert request("/api/archive/transfers/latest", supplied=token, origin="http://evil.example")[0] == 403
                    result["authenticated_archive_read"] = True
                    result["foreign_origin_rejected"] = True
                    # Reproduce the onefile boundary: the worker remains real
                    # and reachable after its bootloader process has exited.
                    proc.kill()
                    proc.wait(timeout=10)
                    assert KERNEL.WaitForSingleObject(worker, 0) == 258
                    assert request("/api/health")[0] == 200
                    result["worker_survives_bootloader_exit"] = True
                    code, status = request("/api/archive/prepare-exit", "POST", {}, supplied=token)
                    assert code == 200 and status == {"safe": True, "task_id": None}, (code, status)
                    result["atomic_exit_check"] = status
                else:
                    assert request("/api/archive/transfers/latest", supplied=token)[0] == 403
                    assert request("/api/settings", "PUT", {}, supplied=token)[0] == 403
                    result["unconfigured_session_fails_closed"] = True
                # Only an isolated, empty fixture worker is ever terminated.
                assert KERNEL.TerminateProcess(worker, 0)
                assert KERNEL.WaitForSingleObject(worker, 10000) == 0
                proc.wait(timeout=10)
                result["worker_reaped"] = True
                result["bootloader_reaped"] = True
            finally:
                if worker:
                    if KERNEL.WaitForSingleObject(worker, 0) == 258:
                        KERNEL.TerminateProcess(worker, 1)
                        KERNEL.WaitForSingleObject(worker, 10000)
                    KERNEL.CloseHandle(worker)
                if proc is not None:
                    if proc.poll() is None:
                        proc.kill()
                    proc.wait(timeout=10)
                    if pump:
                        pump.join(timeout=3)
                    if proc.stdout:
                        proc.stdout.close()
    return result


def run_supervised(timeout_seconds: int) -> dict:
    OUT.mkdir(parents=True, exist_ok=True)
    job = KERNEL.CreateJobObjectW(None, None)
    assert job, ctypes.get_last_error()
    limits = ExtendedLimits()
    limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    assert KERNEL.SetInformationJobObject(job, 9, ctypes.byref(limits), ctypes.sizeof(limits))
    try:
        with fixture_directory() as fixture_root:
            # The runner waits for a line before creating any sidecar. Assign
            # it to the job first, then release it and immediately close stdin.
            proc = subprocess.Popen([sys.executable, str(Path(__file__).resolve()),
                "--fixture-root", str(fixture_root)], stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                encoding="utf-8", errors="replace", creationflags=subprocess.CREATE_NO_WINDOW)
            runner_handle = KERNEL.OpenProcess(0x0100 | 0x0001, False, proc.pid)
            try:
                assert runner_handle, ctypes.get_last_error()
                assert KERNEL.AssignProcessToJobObject(job, runner_handle), ctypes.get_last_error()
                try:
                    output, error = proc.communicate("go\n", timeout=timeout_seconds)
                except subprocess.TimeoutExpired:
                    KERNEL.TerminateJobObject(job, 124)
                    output, error = proc.communicate(timeout=15)
                    raise TimeoutError(f"frozen sidecar checks exceeded {timeout_seconds}s") from None
                if proc.returncode:
                    raise RuntimeError(error or output or f"runner exited {proc.returncode}")
                report = json.loads(output)
            finally:
                # This only targets our own assigned runner/descendants and
                # also catches a failed fixture that lost its worker PID.
                stop_job(job)
                if proc.poll() is None:
                    proc.kill()
                proc.wait(timeout=15)
                if runner_handle:
                    KERNEL.CloseHandle(runner_handle)
                for pipe in (proc.stdin, proc.stdout, proc.stderr):
                    if pipe:
                        pipe.close()
        assert not fixture_root.exists()
        report["temporary_fixtures_removed"] = True
        report["hard_timeout_seconds"] = timeout_seconds
        report["descendants_contained_in_windows_job"] = True
        return report
    finally:
        KERNEL.CloseHandle(job)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path(__file__).with_suffix(".json"))
    parser.add_argument("--timeout-seconds", type=int, default=180)
    parser.add_argument("--fixture-root", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.fixture_root:
        if sys.stdin.readline().strip() != "go":
            raise SystemExit("supervisor did not authorize fixture startup")
        assert args.fixture_root.resolve().parent == Path(tempfile.gettempdir()).resolve()
        assert args.fixture_root.name.startswith("filmpaw-frozen-evidence-")
        started = time.monotonic()
        report = {"scenarios": [scenario(True, args.fixture_root), scenario(False, args.fixture_root)]}
        report["seconds"] = round(time.monotonic() - started, 2)
        report["artifact_bytes"] = EXE.stat().st_size
        report["artifact_sha256"] = hashlib.sha256(EXE.read_bytes()).hexdigest()
        print(json.dumps(report))
    else:
        if not 1 <= args.timeout_seconds <= 1200:
            parser.error("--timeout-seconds must be between 1 and 1200")
        report = run_supervised(args.timeout_seconds)
        report["checked_at_utc"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        args.output.resolve().write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(report, indent=2))
