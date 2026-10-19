"""Owned Windows process proof, including deterministic delayed rundown."""

import sys
import time
import os
import queue
import threading
from uuid import uuid4

import pytest

from filmpaw_server.scan_native import NativeOwner, WindowsAPI, CreationFailed


class NativeModel:
    def __init__(self):
        self.next_handle = 100
        self.births = {10: (10, 1000), 20: (20, 2000)}
        self.listed = [10, 20]
        self.signals = {10: False, 20: False}
        self.closed = []
        self.fail = None
        self.terminated = False

    def create_job(self, name):
        return 1

    def configure_job(self, job):
        assert job == 1

    def pipe(self):
        self.next_handle += 2
        return self.next_handle - 1, self.next_handle

    def inherit(self, handle, value):
        pass

    def create_process(self, argv, env, cwd, job, std):
        assert job == 1 and len(std) == 3  # Job is part of creation, not post-start.
        return 10, 11, 10

    def identity(self, handle, job):
        return self.births[handle]

    def members(self, job):
        if self.fail == "query":
            raise OSError()
        return self.listed

    def open_process(self, pid):
        if self.fail == "open":
            raise OSError()
        return pid

    def terminate_job(self, job):
        if self.fail == "terminate":
            raise OSError()
        self.terminated = True
        self.signals[10] = True

    def wait(self, handle, timeout):
        if self.fail == "wait":
            raise OSError()
        return self.signals.get(handle, True)

    def active(self, job):
        if self.fail == "active":
            raise OSError()
        return 0

    def close(self, handle):
        if self.fail == ("close", handle):
            raise OSError()
        assert handle not in self.closed
        self.closed.append(handle)


def started():
    api = NativeModel()
    owner = NativeOwner(api)
    owner.start(["fixed-worker"], {}, ".")
    return owner, api


def test_active_zero_does_not_release_unsignaled_known_member():
    owner, api = started()
    owner.capture_members()
    assert not owner.settle(time.monotonic() + 1).released
    assert owner.job == 1 and 20 not in api.closed
    api.signals[20] = True
    assert owner.settle(time.monotonic() + 1).released
    assert owner.job is None


def test_failed_open_stays_unknown_after_pid_disappears():
    owner, api = started()
    api.fail = "open"
    assert not owner.settle(time.monotonic() + 1).released
    assert api.terminated
    api.fail, api.listed = None, []
    api.signals[20] = True
    assert not owner.settle(time.monotonic() + 1).released
    assert owner.identity_uncertain and owner.job == 1


def test_partial_close_keeps_only_failed_handles_for_retry():
    owner, api = started()
    api.signals[20] = True
    api.fail = ("close", 20)
    assert not owner.settle(time.monotonic() + 1).released
    prior = list(api.closed)
    api.fail = None
    assert owner.settle(time.monotonic() + 1).released
    assert all(api.closed.count(h) == 1 for h in prior)


def test_io_owner_must_join_before_handles_are_closed():
    owner, api = started()
    api.signals[20] = True

    class Held:
        def join(self, timeout):
            pass

        def is_alive(self):
            return True

    assert not owner.settle(time.monotonic() + 1, threads=(Held(),)).released
    assert owner.job == 1
    assert owner.settle(time.monotonic() + 1).released


@pytest.mark.parametrize("fault", ["terminate", "wait", "active"])
def test_checked_native_fault_retains_same_owner_until_proof_retry(fault):
    owner, api = started()
    api.signals[20] = True
    api.fail = fault
    assert not owner.settle(time.monotonic() + .1).released
    assert owner.job == 1
    api.fail = None
    assert owner.settle(time.monotonic() + 1).released


def test_failed_native_creation_is_distinct_from_unknown_creation():
    api = NativeModel()
    owner = NativeOwner(api)
    def fails(*_):
        raise CreationFailed()
    api.create_process = fails
    api.listed = []
    with pytest.raises(CreationFailed):
        owner.start(["fixed"], {}, ".")
    assert owner.no_process_created
    assert owner.settle(time.monotonic() + 1).released


@pytest.mark.skipif(sys.platform != "win32", reason="Windows named pipe and birth handles")
def test_native_blocked_named_pipe_readfile_exits_with_independent_birth_proof(tmp_path):
    import ctypes
    from ctypes import wintypes as w
    api = WindowsAPI()
    api.k.CreateNamedPipeW.argtypes = [w.LPCWSTR, w.DWORD, w.DWORD, w.DWORD, w.DWORD, w.DWORD, w.DWORD, w.LPVOID]
    api.k.CreateNamedPipeW.restype = w.HANDLE
    name = "\\\\.\\pipe\\filmpaw-scan-test-" + str(uuid4())
    pipe = api.k.CreateNamedPipeW(name, 3, 0, 1, 4096, 4096, 0, None)
    assert pipe and pipe != ctypes.c_void_p(-1).value
    held = tmp_path / "owned-held.bin"
    held.write_bytes(b"synthetic")
    script = '''import ctypes,os,sys
from ctypes import wintypes as w
k=ctypes.WinDLL("kernel32",use_last_error=True)
k.CreateFileW.argtypes=[w.LPCWSTR,w.DWORD,w.DWORD,w.LPVOID,w.DWORD,w.DWORD,w.HANDLE]
k.CreateFileW.restype=w.HANDLE
k.ReadFile.argtypes=[w.HANDLE,w.LPVOID,w.DWORD,ctypes.POINTER(w.DWORD),w.LPVOID]
k.ReadFile.restype=w.BOOL
h=k.CreateFileW(sys.argv[1],0x80000000,1,None,3,0,None)
p=k.CreateFileW(sys.argv[2],0x80000000,0,None,3,0,None)
assert h != ctypes.c_void_p(-1).value and p != ctypes.c_void_p(-1).value
print(os.getpid(),flush=True)
b=ctypes.create_string_buffer(1); n=w.DWORD()
k.ReadFile(p,b,1,ctypes.byref(n),None)
raise SystemExit(42)
'''
    owner, observations, received = NativeOwner(), [], queue.Queue()
    reader = None
    try:
        owner.start([sys.executable, "-c", script, str(held), name], dict(os.environ), str(tmp_path))
        reader = threading.Thread(target=lambda: received.put(owner.read_stdout(100)), daemon=True)
        reader.start()
        pid = int(received.get(timeout=5).strip())
        owner.capture_worker(pid)
        owner.capture_members()
        for member in owner.members:
            handle = api.open_process(member.pid)
            assert api.identity(handle, owner.job) == (member.pid, member.created)
            observations.append(handle)
            assert not api.wait(handle, 0)
        with pytest.raises(PermissionError):
            held.unlink()
        proof = owner.settle(time.monotonic() + 5, threads=(reader,))
        assert proof.released, proof.error_codes
        assert all(api.wait(handle, 0) for handle in observations)
        assert all(member.signaled for member in owner.members)
        held.unlink()
    finally:
        owner.settle(time.monotonic() + 5, threads=(() if reader is None else (reader,)))
        for handle in observations:
            api.close(handle)
        api.close(pipe)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows creation-time Job binding")
def test_native_child_is_job_bound_and_exits_before_held_file_can_be_removed(tmp_path):
    held = tmp_path / "owned.bin"
    held.write_bytes(b"synthetic")
    script = (
        "import ctypes,sys; from ctypes import wintypes as w; "
        "k=ctypes.WinDLL('kernel32',use_last_error=True); "
        "k.CreateFileW.restype=w.HANDLE; "
        "h=k.CreateFileW(sys.argv[1],0x80000000,1,None,3,0,None); "
        "print('ready',flush=True); sys.stdin.buffer.read(1)"
    )
    owner = NativeOwner()
    try:
        owner.start([sys.executable, "-c", script, str(held)], {}, str(tmp_path))
        assert owner.read_stdout(100).strip() == b"ready"
        owner.capture_members()
        assert owner.members
        proof = owner.settle(time.monotonic() + 5)
        assert proof.released, proof.error_codes
        held.unlink()
    finally:
        owner.settle(time.monotonic() + 5)
