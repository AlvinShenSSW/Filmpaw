"""Windows ownership for the fixed scanner. No application or database imports."""

import ctypes
import hashlib
import math
import os
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass


class CreationFailed(OSError):
    """CreateProcess returned FALSE: no child was created."""


class WindowsAPI:
    def __init__(self):
        from ctypes import wintypes as w
        if sys.platform != "win32":
            raise OSError("scanner_platform_unsupported")
        self.k = ctypes.WinDLL("kernel32", use_last_error=True)
        self.w = w
        self.snapshot_handles = set()

        class Security(ctypes.Structure):
            _fields_ = [("length", w.DWORD), ("descriptor", w.LPVOID), ("inherit", w.BOOL)]

        class Startup(ctypes.Structure):
            _fields_ = [("cb", w.DWORD), ("reserved", w.LPWSTR), ("desktop", w.LPWSTR),
                        ("title", w.LPWSTR)] + [(name, w.DWORD) for name in
                        ("x", "y", "width", "height", "columns", "rows", "fill", "flags")] + [
                        ("show", w.WORD), ("reserved_size", w.WORD), ("reserved_bytes", w.LPVOID),
                        ("stdin", w.HANDLE), ("stdout", w.HANDLE), ("stderr", w.HANDLE)]

        class StartupEx(ctypes.Structure):
            _fields_ = [("info", Startup), ("attributes", w.LPVOID)]

        class ProcessInfo(ctypes.Structure):
            _fields_ = [("process", w.HANDLE), ("thread", w.HANDLE), ("pid", w.DWORD), ("tid", w.DWORD)]

        class Limits(ctypes.Structure):
            _fields_ = [("process_time", ctypes.c_longlong), ("job_time", ctypes.c_longlong),
                        ("flags", w.DWORD), ("min_ws", ctypes.c_size_t), ("max_ws", ctypes.c_size_t),
                        ("active", w.DWORD), ("affinity", ctypes.c_size_t), ("priority", w.DWORD),
                        ("scheduling", w.DWORD)]

        class Extended(ctypes.Structure):
            _fields_ = [("basic", Limits), ("io", ctypes.c_ulonglong * 6),
                        ("memory", ctypes.c_size_t * 4)]

        class Accounting(ctypes.Structure):
            _fields_ = [("times", ctypes.c_longlong * 4), ("faults", w.DWORD),
                        ("total", w.DWORD), ("active", w.DWORD), ("terminated", w.DWORD)]

        class Members(ctypes.Structure):
            _fields_ = [("assigned", w.DWORD), ("count", w.DWORD), ("pids", ctypes.c_size_t * 32)]

        class ProcessEntry(ctypes.Structure):
            _fields_ = [("size", w.DWORD), ("usage", w.DWORD), ("pid", w.DWORD),
                        ("heap", ctypes.c_size_t), ("module", w.DWORD), ("threads", w.DWORD),
                        ("parent", w.DWORD), ("priority", w.LONG), ("flags", w.DWORD),
                        ("exe", w.WCHAR * 260)]

        self.Security, self.StartupEx, self.ProcessInfo = Security, StartupEx, ProcessInfo
        self.Extended, self.Accounting, self.Members = Extended, Accounting, Members
        self.ProcessEntry = ProcessEntry
        signatures = (
            ("CreateJobObjectW", [w.LPVOID, w.LPCWSTR], w.HANDLE),
            ("OpenJobObjectW", [w.DWORD, w.BOOL, w.LPCWSTR], w.HANDLE),
            ("SetInformationJobObject", [w.HANDLE, ctypes.c_int, w.LPVOID, w.DWORD], w.BOOL),
            ("QueryInformationJobObject", [w.HANDLE, ctypes.c_int, w.LPVOID, w.DWORD, w.LPVOID], w.BOOL),
            ("TerminateJobObject", [w.HANDLE, w.UINT], w.BOOL),
            ("CreatePipe", [ctypes.POINTER(w.HANDLE), ctypes.POINTER(w.HANDLE), w.LPVOID, w.DWORD], w.BOOL),
            ("SetHandleInformation", [w.HANDLE, w.DWORD, w.DWORD], w.BOOL),
            ("InitializeProcThreadAttributeList", [w.LPVOID, w.DWORD, w.DWORD, ctypes.POINTER(ctypes.c_size_t)], w.BOOL),
            ("UpdateProcThreadAttribute", [w.LPVOID, w.DWORD, ctypes.c_size_t, w.LPVOID, ctypes.c_size_t, w.LPVOID, w.LPVOID], w.BOOL),
            ("DeleteProcThreadAttributeList", [w.LPVOID], None),
            ("CreateProcessW", [w.LPCWSTR, w.LPWSTR, w.LPVOID, w.LPVOID, w.BOOL, w.DWORD,
                                w.LPVOID, w.LPCWSTR, w.LPVOID, ctypes.POINTER(ProcessInfo)], w.BOOL),
            ("OpenProcess", [w.DWORD, w.BOOL, w.DWORD], w.HANDLE),
            ("ProcessIdToSessionId", [w.DWORD, ctypes.POINTER(w.DWORD)], w.BOOL),
            ("CreateToolhelp32Snapshot", [w.DWORD, w.DWORD], w.HANDLE),
            ("Process32FirstW", [w.HANDLE, ctypes.POINTER(ProcessEntry)], w.BOOL),
            ("Process32NextW", [w.HANDLE, ctypes.POINTER(ProcessEntry)], w.BOOL),
            ("GetProcessId", [w.HANDLE], w.DWORD),
            ("GetCurrentProcess", [], w.HANDLE),
            ("GetProcessTimes", [w.HANDLE] + [ctypes.POINTER(w.FILETIME)] * 4, w.BOOL),
            ("IsProcessInJob", [w.HANDLE, w.HANDLE, ctypes.POINTER(w.BOOL)], w.BOOL),
            ("WaitForSingleObject", [w.HANDLE, w.DWORD], w.DWORD),
            ("CloseHandle", [w.HANDLE], w.BOOL),
            ("ReadFile", [w.HANDLE, w.LPVOID, w.DWORD, ctypes.POINTER(w.DWORD), w.LPVOID], w.BOOL),
            ("WriteFile", [w.HANDLE, w.LPVOID, w.DWORD, ctypes.POINTER(w.DWORD), w.LPVOID], w.BOOL),
        )
        for name, args, result in signatures:
            fn = getattr(self.k, name)
            fn.argtypes, fn.restype = args, result

    def check(self, value):
        if not value:
            raise ctypes.WinError(ctypes.get_last_error())
        return value

    def create_job(self, name):
        return int(self.check(self.k.CreateJobObjectW(None, name)))

    def configure_job(self, job):
        info = self.Extended()
        info.basic.flags = 0x2000  # KILL_ON_JOB_CLOSE; neither breakaway flag.
        self.check(self.k.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info)))

    def pipe(self):
        first, second = self.w.HANDLE(), self.w.HANDLE()
        self.check(self.k.CreatePipe(ctypes.byref(first), ctypes.byref(second), None, 0))
        return first.value, second.value

    def inherit(self, handle, value):
        self.check(self.k.SetHandleInformation(handle, 1, int(value)))

    def create_process(self, argv, env, cwd, job, std):
        size = ctypes.c_size_t()
        self.k.InitializeProcThreadAttributeList(None, 2, 0, ctypes.byref(size))
        if not size.value:
            raise OSError("scanner_attribute_setup")
        buffer = ctypes.create_string_buffer(size.value)
        self.check(self.k.InitializeProcThreadAttributeList(buffer, 2, 0, ctypes.byref(size)))
        try:
            jobs = (self.w.HANDLE * 1)(job)
            handles = (self.w.HANDLE * 3)(*std)
            for key, value in ((0x2000D, jobs), (0x20002, handles)):
                self.check(self.k.UpdateProcThreadAttribute(buffer, 0, key, value,
                                                           ctypes.sizeof(value), None, None))
            startup = self.StartupEx()
            startup.info.cb = ctypes.sizeof(startup)
            startup.info.flags = 0x100  # STARTF_USESTDHANDLES
            startup.info.stdin, startup.info.stdout, startup.info.stderr = std
            startup.attributes = ctypes.cast(buffer, self.w.LPVOID)
            command = ctypes.create_unicode_buffer(subprocess.list2cmdline(argv))
            environment = ctypes.create_unicode_buffer("\0".join(f"{k}={v}" for k, v in sorted(env.items())) + "\0\0")
            info = self.ProcessInfo()
            if not self.k.CreateProcessW(argv[0], command, None, None, True,
                       0x80000 | 0x400 | 0x08000000, environment, cwd,
                       ctypes.byref(startup), ctypes.byref(info)):
                raise CreationFailed("scanner_create_failed")
            return info.process, info.thread, info.pid
        finally:
            self.k.DeleteProcThreadAttributeList(buffer)

    def open_process(self, pid):
        return int(self.check(self.k.OpenProcess(0x101000, False, pid)))

    def process_birth(self, handle):
        pid = self.check(self.k.GetProcessId(handle))
        times = [self.w.FILETIME() for _ in range(4)]
        self.check(self.k.GetProcessTimes(handle, *(ctypes.byref(t) for t in times)))
        return int(pid), (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime

    def runtime_identity(self):
        import winreg
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Cryptography",
                            0, winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as key:
            machine, kind = winreg.QueryValueEx(key, "MachineGuid")
        if kind != winreg.REG_SZ or not isinstance(machine, str):
            raise OSError("scanner_machine_identity_unknown")
        try:
            machine = str(uuid.UUID(machine))
        except ValueError:
            raise OSError("scanner_machine_identity_unknown") from None
        # A pseudo HANDLE has no acquired-resource lifetime to close/recover.
        pid, created = self.process_birth(self.k.GetCurrentProcess())
        session = self.w.DWORD()
        self.check(self.k.ProcessIdToSessionId(pid, ctypes.byref(session)))
        return {"pid": pid, "created_filetime_100ns": created, "session_id": session.value,
                "machine_digest": hashlib.sha256(b"Filmpaw/runtime-machine/v1\0" + machine.encode("ascii")).hexdigest()}

    def open_job(self, name):
        return int(self.check(self.k.OpenJobObjectW(4, False, name)))

    def process_ids(self):
        """Complete bounded Toolhelp observation; no partial list proves absence."""
        handle = self.k.CreateToolhelp32Snapshot(2, 0)
        if handle == ctypes.c_void_p(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())
        self.snapshot_handles.add(handle)
        try:
            entry = self.ProcessEntry()
            entry.size = ctypes.sizeof(entry)
            ids = []
            ok = self.k.Process32FirstW(handle, ctypes.byref(entry))
            while ok:
                if len(ids) >= 65536:
                    raise OSError("scanner_process_snapshot_bound")
                ids.append(int(entry.pid))
                ok = self.k.Process32NextW(handle, ctypes.byref(entry))
            if ctypes.get_last_error() != 18:
                raise ctypes.WinError(ctypes.get_last_error())
            return tuple(ids)
        finally:
            self.close(handle)
            self.snapshot_handles.remove(handle)

    def identity(self, handle, job):
        pid, created = self.process_birth(handle)
        member = self.w.BOOL()
        self.check(self.k.IsProcessInJob(handle, job, ctypes.byref(member)))
        if not member.value:
            raise OSError("scanner_wrong_job")
        return pid, created

    def members(self, job):
        result = self.Members()
        self.check(self.k.QueryInformationJobObject(job, 3, ctypes.byref(result), ctypes.sizeof(result), None))
        if result.count != result.assigned or result.count > 32:
            raise OSError("scanner_member_overflow")
        return [int(result.pids[i]) for i in range(result.count)]

    def active(self, job):
        result = self.Accounting()
        self.check(self.k.QueryInformationJobObject(job, 1, ctypes.byref(result), ctypes.sizeof(result), None))
        return int(result.active)

    def wait(self, handle, timeout):
        result = self.k.WaitForSingleObject(handle, min(0xFFFFFFFE, math.ceil(max(0, timeout) * 1000)))
        if result not in (0, 258):
            raise OSError("scanner_wait_failed")
        return result == 0

    def terminate_job(self, job):
        self.check(self.k.TerminateJobObject(job, 1))

    def close(self, handle):
        self.check(self.k.CloseHandle(handle))

    def read(self, handle, count):
        buffer, size = ctypes.create_string_buffer(count), self.w.DWORD()
        if not self.k.ReadFile(handle, buffer, count, ctypes.byref(size), None):
            if ctypes.get_last_error() in (109, 232):
                return b""
            self.check(False)
        return buffer.raw[:size.value]

    def write(self, handle, value):
        size = self.w.DWORD()
        buffer = ctypes.create_string_buffer(value)
        self.check(self.k.WriteFile(handle, buffer, len(value), ctypes.byref(size), None))
        return size.value


@dataclass
class Member:
    pid: int
    handle: int | None
    created: int | None = None
    role: str = "member"
    signaled: bool = False


@dataclass(frozen=True)
class NativeExit:
    released: bool
    job_empty: bool
    io_joined: bool
    handles_closed: bool
    error_codes: tuple[str, ...]


class NativeOwner:
    def __init__(self, api=None):
        self.api = api
        self.job_name = "Local\\FilmpawScan-" + str(uuid.uuid4())
        self.job = None
        self.pid = 0
        self.handles = set()
        self.members = []
        self.identity_uncertain = False
        self.unresolved_member_pids = set()
        self.exit_proven = False
        self.stdin = self.stdout = self.stderr = None
        self.no_process_created = True

    def start(self, argv, env, cwd):
        if self.handles or self.job is not None:
            raise OSError("scanner_owner_retained")
        self.api = self.api or WindowsAPI()
        self.job = self.api.create_job(self.job_name)
        self.api.configure_job(self.job)
        pipes = []
        for _ in range(3):
            pair = self.api.pipe()
            self.handles.update(pair)
            pipes.append(pair)
        child = [pipes[0][0], pipes[1][1], pipes[2][1]]
        self.stdin, self.stdout, self.stderr = pipes[0][1], pipes[1][0], pipes[2][0]
        for handle in self.handles:
            self.api.inherit(handle, handle in child)
        self.no_process_created = False
        try:
            process, thread, self.pid = self.api.create_process(argv, env, cwd, self.job, child)
        except CreationFailed:
            self.no_process_created = True
            raise
        except OSError:
            self.identity_uncertain = True
            raise
        self.handles.update((process, thread))
        member = Member(self.pid, process, role="launcher")
        self.members.append(member)
        try:
            self._identity(member)
        except OSError:
            self.identity_uncertain = True
            raise
        for handle in [*child, thread]:
            self._close(handle)

    def _identity(self, member):
        pid, created = self.api.identity(member.handle, self.job)
        if pid != member.pid or type(created) is not int or not 0 < created < 1 << 64:
            raise OSError("scanner_invalid_birth")
        member.created = created

    def capture_worker(self, pid):
        if type(pid) is not int or not 0 < pid <= 0xFFFFFFFF:
            raise OSError("scanner_invalid_pid")
        member = Member(pid, None, role="worker")
        self.members.append(member)
        try:
            member.handle = self.api.open_process(pid)
            self.handles.add(member.handle)
            self._identity(member)
        except OSError:
            self.identity_uncertain = True
            self.unresolved_member_pids.add(pid)
            raise
        return member

    def capture_members(self):
        if self.exit_proven or self.job is None:
            return
        try:
            pids = self.api.members(self.job)
            if len(pids) > 32 or len(pids) != len(set(pids)):
                raise OSError("scanner_invalid_members")
            for pid in pids:
                if any(m.pid == pid for m in self.members):
                    continue
                if type(pid) is not int or not 0 < pid <= 0xFFFFFFFF or len(self.members) >= 34:
                    raise OSError("scanner_invalid_members")
                self.unresolved_member_pids.add(pid)
                member = Member(pid, None)
                self.members.append(member)
                member.handle = self.api.open_process(pid)
                self.handles.add(member.handle)
                self._identity(member)
                self.unresolved_member_pids.remove(pid)
        except OSError:
            self.identity_uncertain = True
            raise

    def read_stdout(self, count=4096):
        return self.api.read(self.stdout, count)

    def read_stderr(self, count=4096):
        return self.api.read(self.stderr, count)

    def write(self, value):
        offset = 0
        while offset < len(value):
            count = self.api.write(self.stdin, value[offset:offset + 4096])
            if not count:
                raise OSError("scanner_pipe_write")
            offset += count

    def _close(self, handle):
        self.api.close(handle)
        self.handles.remove(handle)
        for member in self.members:
            if member.handle == handle:
                member.handle = None

    def settle(self, deadline, *, threads=(), terminate=True):
        errors = []
        if self.api is None:
            return NativeExit(True, True, True, True, ())
        empty = self.exit_proven
        if not self.exit_proven:
            try:
                self.capture_members()
            except OSError:
                errors.append("member_identity_unknown")
            if self.job is not None and terminate:
                try:
                    self.api.terminate_job(self.job)
                except OSError:
                    errors.append("terminate_failed")
            for member in self.members:
                if not member.signaled and member.handle is not None:
                    try:
                        member.signaled = self.api.wait(member.handle, max(0, deadline - time.monotonic()))
                    except OSError:
                        errors.append("wait_failed")
            try:
                empty = self.job is None or self.api.active(self.job) == 0
            except OSError:
                errors.append("job_query_failed")
            if not empty or any(not m.signaled for m in self.members):
                errors.append("process_exit_unknown")
        for thread in threads:
            thread.join(max(0, deadline - time.monotonic()))
        joined = not any(t.is_alive() for t in threads)
        if not joined:
            errors.append("io_owner_unknown")
        if self.identity_uncertain:
            errors.append("member_identity_unknown")
        if not errors:
            self.exit_proven = True
            for handle in tuple(self.handles):
                try:
                    self._close(handle)
                except OSError:
                    errors.append("handle_close_failed")
            if not self.handles and self.job is not None:
                try:
                    self.api.close(self.job)
                    self.job = None
                except OSError:
                    errors.append("job_close_failed")
        closed = not self.handles and self.job is None
        return NativeExit(not errors and closed, empty, joined, closed, tuple(dict.fromkeys(errors)))
