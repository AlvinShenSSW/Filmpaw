"""Durable, one-way archive queues. Filesystem I/O never holds the DB lock.

Each movie is copied to an exclusively owned directory, read back and hashed,
committed without replacement, then cleaned one verified source file at a time.
There is deliberately no recursive deletion, overwrite, or automatic resume.
"""

import copy
import ctypes
import hashlib
import json
import os
import shutil
import sqlite3
import stat
import sys
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from filmpaw_server.directory_reads import directory_error_retryable


CHUNK_SIZE = 1024 * 1024
STAGING_PREFIX = ".filmpaw-transfer-"
ACTIVE = {"queued", "running"}


class TransferError(Exception):
    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.status_code = status_code


class _Stopped(Exception):
    pass


class _Conflict(TransferError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _name(name: str) -> str:
    if (
        not name or name in {".", ".."} or name.rstrip(" .") != name
        or any(c in name for c in '/\\:\x00<>"|?*')
        or name.startswith(STAGING_PREFIX)
    ):
        raise TransferError("无效的影片或演员目录名")
    return name


def _identity(value: os.stat_result) -> list[int]:
    return [value.st_dev, value.st_ino]


def _fingerprint(value: os.stat_result) -> list[int]:
    # Windows lstat/fstat disagree on ctime's birth/change-time semantics in
    # Python 3.13. Identity + size + mtime are stable across both calls; hashes
    # additionally protect against content changes with restored timestamps.
    return [value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns]


def _plain_stat(path: Path) -> os.stat_result:
    value = path.lstat()
    if stat.S_ISLNK(value.st_mode) or getattr(value, "st_file_attributes", 0) & 0x400:
        raise TransferError(f"不支持符号链接或重解析点：{path.name}")
    if not (stat.S_ISDIR(value.st_mode) or stat.S_ISREG(value.st_mode)):
        raise TransferError(f"不支持特殊文件：{path.name}")
    return value


def _directory(path: Path, expected: list[int] | None = None) -> list[int]:
    # Check the original path before resolve(): resolving first would hide a
    # junction inside an otherwise approved folder.
    path = path.absolute()
    for parent in reversed(path.parents):
        _plain_stat(parent)
    value = _plain_stat(path)
    if not stat.S_ISDIR(value.st_mode):
        raise TransferError(f"目录不可访问：{path}")
    actual = _identity(value)
    if expected is not None and actual != expected:
        raise TransferError(f"目录身份已变化，请重新选择：{path}", 409)
    return actual


def _nested(first: Path, second: Path) -> bool:
    a, b = os.path.normcase(str(first)), os.path.normcase(str(second))
    try:
        return os.path.commonpath([a, b]) in {a, b}
    except ValueError:
        return False


def _manifest(root: Path) -> dict:
    """Inventory with identities, including empty directories, without links."""
    _directory(root)
    result = {}
    folded = set()
    pending = [root]
    while pending:
        path = pending.pop()
        value = _plain_stat(path)
        relative = path.relative_to(root).as_posix()
        key = "" if relative == "." else relative
        if key.casefold() in folded:
            raise TransferError(f"目录包含大小写等价的重名内容：{key}")
        folded.add(key.casefold())
        is_dir = stat.S_ISDIR(value.st_mode)
        result[key] = {
            "kind": "dir" if is_dir else "file", "identity": _identity(value),
            "fingerprint": _fingerprint(value), "size": 0 if is_dir else value.st_size,
            "mtime_ns": value.st_mtime_ns, "atime_ns": value.st_atime_ns,
        }
        if is_dir:
            with os.scandir(path) as entries:
                pending.extend(Path(e.path) for e in sorted(entries, key=lambda e: e.name, reverse=True))
    return result


def _check_manifest(root: Path, expected: dict, *, identities: bool) -> None:
    actual = _manifest(root)
    if actual.keys() != expected.keys():
        raise TransferError("影片内容清单已变化，保留本地源文件")
    for relative, item in expected.items():
        other = actual[relative]
        if item["kind"] != other["kind"] or item["size"] != other["size"]:
            raise TransferError(f"影片内容已变化：{relative}")
        if identities:
            if item["identity"] != other["identity"]:
                raise TransferError(f"源文件或目录已替换：{relative}")
            if item["kind"] == "file" and item["fingerprint"] != other["fingerprint"]:
                raise TransferError(f"源文件已修改：{relative}")


@contextmanager
def _directory_guard(path: Path, expected: list[int] | None = None, *, committing: bool = False):
    """Keep every Windows ancestor from being renamed/replaced during I/O.

    Checking lstat alone leaves a check/open gap where a directory can become
    a junction. Denying WRITE sharing also blocks FSCTL_SET_REPARSE_POINT from
    changing an already-open empty directory in place. The immediate commit
    parent needs WRITE sharing for rename; it remains nonempty because the
    staged directory is held by the native rename handle until commit finishes.
    """
    if sys.platform != "win32":
        _directory(path, expected)
        yield
        return
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    create = kernel.CreateFileW
    create.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                       wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    create.restype = wintypes.HANDLE
    close = kernel.CloseHandle
    close.argtypes = [wintypes.HANDLE]
    handles = []
    path = path.absolute()
    try:
        for part in [*reversed(path.parents), path]:
            # FILE_LIST_DIRECTORY participates in Windows sharing checks;
            # READ_ATTRIBUTES alone is metadata-only and would not block rename.
            share = 3 if committing and part == path else 1
            handle = create(str(part), 0x81, share, None, 3, 0x02200000, None)
            if handle == ctypes.c_void_p(-1).value:
                raise ctypes.WinError(ctypes.get_last_error())
            handles.append(handle)
            value = _plain_stat(part)
            if not stat.S_ISDIR(value.st_mode):
                raise TransferError(f"目录已变化：{part}")
        if expected is not None and _identity(value) != expected:
            raise TransferError(f"目录身份已变化：{path}", 409)
        yield
    finally:
        for handle in reversed(handles):
            close(handle)


@contextmanager
def _open_read(path: Path, *, deleting: bool = False):
    """Windows denies concurrent write/delete while hashing this exact file.

    Cleanup requests DELETE access up front and marks this same handle for
    deletion, eliminating a hash-close-unlink window on the shipped platform.
    """
    _plain_stat(path)
    if sys.platform != "win32":
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(fd, "rb") as stream:
            yield stream
        return
    import msvcrt
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    create = kernel.CreateFileW
    create.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                       wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    create.restype = wintypes.HANDLE
    handle = create(str(path), 0x80000000 | (0x10000 if deleting else 0), 1, None, 3, 0x00200000, None)
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        fd = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
    except BaseException:
        close = kernel.CloseHandle
        close.argtypes = [wintypes.HANDLE]
        close(handle)
        raise
    with os.fdopen(fd, "rb") as stream:
        yield stream


def _hash(stream, progress=None) -> str:
    digest = hashlib.sha256()
    while chunk := stream.read(CHUNK_SIZE):
        digest.update(chunk)
        if progress:
            progress(len(chunk))
    return digest.hexdigest()


def _windows_directory_change(path: Path, expected: list[int], target: Path | None = None) -> None:
    """Rename or remove the directory identified by the same locked handle."""
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    create = kernel.CreateFileW
    create.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                       wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    create.restype = wintypes.HANDLE
    close = kernel.CloseHandle
    close.argtypes = [wintypes.HANDLE]
    handle = create(str(path), 0x10081, 1, None, 3, 0x02200000, None)
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        _directory(path, expected)
        set_info = kernel.SetFileInformationByHandle
        set_info.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        set_info.restype = wintypes.BOOL
        if target is None:
            delete = ctypes.c_ubyte(1)
            ok = set_info(handle, 4, ctypes.byref(delete), ctypes.sizeof(delete))
        else:
            class RenameInfo(ctypes.Structure):
                _fields_ = [("ReplaceIfExists", ctypes.c_ubyte), ("RootDirectory", wintypes.HANDLE),
                            ("FileNameLength", wintypes.DWORD), ("FileName", wintypes.WCHAR * 1)]
            name = str(target.absolute()).encode("utf-16-le")
            # The Win32 wrapper also expects a terminating WCHAR even though
            # FileNameLength counts only the filename bytes.
            length = RenameInfo.FileName.offset + len(name) + 2
            buffer = ctypes.create_string_buffer(max(length, ctypes.sizeof(RenameInfo)))
            info = RenameInfo.from_buffer(buffer)
            info.ReplaceIfExists = 0
            info.RootDirectory = None
            info.FileNameLength = len(name)
            ctypes.memmove(ctypes.addressof(buffer) + RenameInfo.FileName.offset, name, len(name))
            ok = set_info(handle, 3, buffer, length)
        if not ok:
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        close(handle)


def _rename_no_replace(source: Path, target: Path, identity: list[int]) -> None:
    if sys.platform == "win32":
        _windows_directory_change(source, identity, target)
        return
    if sys.platform.startswith("linux"):
        libc = ctypes.CDLL(None, use_errno=True)
        rename = getattr(libc, "renameat2", None)
        if rename is None:
            raise OSError("This filesystem does not support no-replace commit")
        rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
        rename.restype = ctypes.c_int
        if rename(-100, os.fsencode(source), -100, os.fsencode(target), 1):
            error = ctypes.get_errno()
            raise OSError(error, os.strerror(error), str(target))
        return
    raise OSError("This platform does not support no-replace commit")


def _delete_exact(path: Path, destination: Path, entry: dict) -> None:
    with _open_read(destination) as target_stream, _open_read(path, deleting=True) as source_stream:
        if _fingerprint(os.fstat(source_stream.fileno())) != entry["fingerprint"]:
            raise TransferError(f"源文件已变化，未清理：{path.name}")
        if _hash(target_stream) != entry["sha256"] or _hash(source_stream) != entry["sha256"]:
            raise TransferError(f"内容核对失败，未清理：{path.name}")
        if _identity(_plain_stat(path)) != entry["identity"]:
            raise TransferError(f"源文件已替换，未清理：{path.name}")
        if sys.platform == "win32":
            import msvcrt
            from ctypes import wintypes
            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            set_info = kernel.SetFileInformationByHandle
            set_info.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
            set_info.restype = wintypes.BOOL
            # FILE_DISPOSITION_INFO.DeleteFile is BOOLEAN (one byte).
            delete = ctypes.c_ubyte(1)
            if not set_info(msvcrt.get_osfhandle(source_stream.fileno()), 4, ctypes.byref(delete), ctypes.sizeof(delete)):
                raise ctypes.WinError(ctypes.get_last_error())
        else:
            # Development/CI only; production uses the handle-bound delete.
            path.unlink()


class TransferService:
    def __init__(self, conn: sqlite3.Connection, db_lock, *, auto_start: bool = True):
        self.conn = conn
        self.db_lock = db_lock
        self._admission = threading.RLock()
        self._contexts = {}
        self._worker = None
        self._stop = threading.Event()
        self._closing = False
        self._auto_start = auto_start
        self._fatal_task = None
        self._working_task = None
        self._context_read_lock = threading.Lock()

    def _read_binding(self, local_dir: str, subdir: str, performer_id: str) -> dict:
        _name(subdir)
        with self.db_lock:
            setting = self.conn.execute("SELECT value FROM settings WHERE key='last_local_dir'").fetchone()
            revision = self.conn.execute(
                "SELECT value FROM settings WHERE key='last_local_dir_revision'"
            ).fetchone()
            performer = self.conn.execute(
                "SELECT p.unc_path,p.name,p.is_missing,p.thumb IS NOT NULL AS has_thumb,s.label AS source_label"
                " FROM performers p JOIN sources s ON s.id=p.source_id WHERE p.id=?", (performer_id,)
            ).fetchone()
        if setting is None or not setting["value"] or setting["value"] != local_dir:
            raise TransferError("本地 batch 目录已变化，请重新选择目录", 409)
        if performer is None:
            raise TransferError("所选演员记录已不存在", 404)
        if performer["is_missing"]:
            raise TransferError("所选演员目录已失效，请刷新片库", 409)
        batch = Path(local_dir).absolute()
        source = batch / subdir
        target = Path(performer["unc_path"]).absolute()
        if _nested(source, target):
            raise TransferError("本地源与 NAS 目标不能相同或互相嵌套", 409)
        return {
            "local_dir": local_dir, "subdir": subdir, "performer_id": performer_id,
            "source_path": str(source), "target_path": str(target),
            "performer_name": performer["name"], "source_label": performer["source_label"],
            "has_thumb": bool(performer["has_thumb"]),
            "_batch_revision": revision["value"] if revision else "",
        }

    def _list(self, path: Path) -> list[dict]:
        result = []
        with os.scandir(path) as entries:
            for entry in entries:
                if entry.name.startswith(STAGING_PREFIX):
                    continue
                info = entry.stat(follow_symlinks=False)
                if stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
                    error = None
                    try:
                        _plain_stat(Path(entry.path))
                        _name(entry.name)
                    except (OSError, TransferError) as exc:
                        if directory_error_retryable(exc):
                            raise
                        error = str(exc)
                    result.append({"name": entry.name, "size_bytes": None, "files": None, "error": error})
        return sorted(result, key=lambda row: row["name"].casefold())

    def create_context(self, local_dir: str, subdir: str, performer_id: str) -> dict:
        binding = self._read_binding(local_dir, subdir, performer_id)
        # Aborting fetch does not cancel a blocking Windows SMB call. Refuse
        # another filesystem read while that first request is still running;
        # bounded client retries must not accumulate abandoned worker threads.
        if not self._context_read_lock.acquire(blocking=False):
            raise TransferError("目录仍在读取，请稍候重试", 503)
        try:
            return self._create_context(binding)
        finally:
            self._context_read_lock.release()

    def _create_context(self, binding: dict) -> dict:
        context = {"id": str(uuid.uuid4()), **{k: v for k, v in binding.items() if not k.startswith("_")}}
        for side in ("source", "target"):
            context[f"{side}_retryable"] = False
            path = Path(binding[f"{side}_path"])
            try:
                identity = _directory(path)
                if side == "source":
                    binding["batch_identity"] = _directory(Path(binding["local_dir"]))
                binding[f"{side}_identity"] = identity
                context[f"{side}_items"] = self._list(path)
                context[f"{side}_error"] = None
            except (OSError, TransferError) as exc:
                context[f"{side}_items"] = []
                context[f"{side}_error"] = str(exc)
                context[f"{side}_retryable"] = directory_error_retryable(exc)
        with self._admission:
            if self._closing:
                raise TransferError("应用正在退出，无法建立新归档上下文", 409)
            # Contexts are process-local capabilities. Old contexts are bounded
            # and never reconstructed from arbitrary client paths.
            if len(self._contexts) >= 128:
                self._contexts.pop(next(iter(self._contexts)))
            self._contexts[context["id"]] = {"binding": binding, "public": copy.deepcopy(context)}
        return context

    def _check_binding(self, binding: dict) -> None:
        _directory(Path(binding["local_dir"]), binding["batch_identity"])
        _directory(Path(binding["source_path"]), binding["source_identity"])
        _directory(Path(binding["target_path"]), binding["target_identity"])
        if _nested(Path(binding["source_path"]), Path(binding["target_path"])):
            raise TransferError("本地源与 NAS 目标不能相同或互相嵌套", 409)

    def _load(self, task_id: str | None = None, request_id: str | None = None) -> dict | None:
        with self.db_lock:
            if task_id is not None:
                row = self.conn.execute("SELECT payload FROM archive_tasks WHERE id=?", (task_id,)).fetchone()
            elif request_id is not None:
                row = self.conn.execute("SELECT payload FROM archive_tasks WHERE request_id=?", (request_id,)).fetchone()
            else:
                row = self.conn.execute("SELECT payload FROM archive_tasks ORDER BY rowid DESC LIMIT 1").fetchone()
        return json.loads(row["payload"]) if row else None

    def _save(self, task: dict) -> None:
        self._working_task = task
        task["updated_at"] = _now()
        if self._stop.is_set() and task["status"] in ACTIVE:
            task["stop_requested"] = True
        with self.db_lock:
            try:
                self.conn.execute(
                    "INSERT INTO archive_tasks(id,request_id,payload) VALUES (?,?,?)"
                    " ON CONFLICT(id) DO UPDATE SET payload=excluded.payload",
                    (task["id"], task["request_id"], json.dumps(task, ensure_ascii=False)),
                )
                self.conn.commit()
            except Exception:
                self.conn.rollback()
                raise

    @staticmethod
    def _public(task: dict) -> dict:
        result = {k: copy.deepcopy(v) for k, v in task.items() if not k.startswith("_") and k != "items"}
        result["items"] = [
            {k: item[k] for k in ("name", "status", "size_bytes", "progress_bytes", "message")}
            for item in task["items"]
        ]
        return result

    def get_task(self, task_id: str | None = None) -> dict | None:
        with self._admission:
            if self._fatal_task and (task_id is None or task_id == self._fatal_task["id"]):
                return self._public(self._fatal_task)
        task = self._load(task_id)
        if task_id is not None and task is None:
            raise TransferError("传输任务不存在", 404)
        if task and task["status"] in ACTIVE and self._stop.is_set():
            task["stop_requested"] = True
        return self._public(task) if task else None

    def _active(self) -> dict | None:
        with self.db_lock:
            rows = self.conn.execute("SELECT payload FROM archive_tasks ORDER BY rowid DESC").fetchall()
        return next((task for row in rows if (task := json.loads(row["payload"]))["status"] in ACTIVE), None)

    def submit(self, context_id: str, names: list[str], request_id: str) -> dict:
        if not request_id or len(request_id) > 128:
            raise TransferError("无效的幂等请求标识")
        if not names or len(names) > 1000:
            raise TransferError("请选择 1 至 1000 部影片")
        names = [_name(name) for name in names]
        if len({name.casefold() for name in names}) != len(names):
            raise TransferError("影片选择包含重复名称")
        with self._admission:
            if self._fatal_task:
                raise TransferError("任务状态保存失败，请重新启动应用并核对上次传输", 409)
            previous = self._load(request_id=request_id)
            if previous:
                if previous["context_id"] != context_id or [i["name"] for i in previous["items"]] != names:
                    raise TransferError("幂等标识已用于另一组传输选择", 409)
                return self._public(previous)
            if self._closing:
                raise TransferError("应用正在退出，不能开始传输", 409)
            if self._active() or (self._worker and self._worker.is_alive()):
                raise TransferError("已有传输任务正在进行", 409)
            context = self._contexts.get(context_id)
            if context is None:
                raise TransferError("归档上下文已失效，请刷新列表", 409)
            public, binding = context["public"], context["binding"]
            if public["source_error"] or public["target_error"]:
                raise TransferError("源或目标目录不可用，请刷新后重试", 409)
            current = self._read_binding(binding["local_dir"], binding["subdir"], binding["performer_id"])
            if current["_batch_revision"] != binding["_batch_revision"]:
                raise TransferError("batch 目录选择已变化，原归档上下文失效，请刷新列表", 409)
            if current["target_path"] != binding["target_path"]:
                raise TransferError("NAS 演员目录已变化，请重新选择目标", 409)
            self._check_binding(binding)
            available = {item["name"] for item in public["source_items"] if item["error"] is None}
            if not set(names).issubset(available):
                raise TransferError("影片不属于当前列表，请刷新后重新选择", 409)
            task = {
                "id": str(uuid.uuid4()), "request_id": request_id, "context_id": context_id,
                **{k: v for k, v in binding.items() if not k.endswith("_identity") and not k.startswith("_")},
                "status": "queued", "phase": "preflight", "current_name": None,
                "progress_bytes": 0, "total_bytes": 0, "verified_bytes": 0,
                "completed_count": 0, "total_count": len(names), "conflicts": [],
                "message": "正在完整预检所选影片", "stop_requested": False,
                "created_at": _now(), "updated_at": _now(), "_binding": copy.deepcopy(binding),
                "items": [{"name": name, "status": "pending", "size_bytes": 0,
                           "progress_bytes": 0, "message": "等待预检"} for name in names],
            }
            self._stop.clear()
            self._save(task)
            if self._auto_start:
                self._worker = threading.Thread(target=self._run, args=(task["id"],), daemon=True, name="archive-transfer")
                self._worker.start()
            return self._public(task)

    def stop(self, task_id: str) -> dict:
        with self._admission:
            if self._fatal_task and self._fatal_task["id"] == task_id:
                return self._public(self._fatal_task)
            task = self._load(task_id)
            if task is None:
                raise TransferError("传输任务不存在", 404)
            if task["status"] in ACTIVE:
                self._stop.set()
                # Only the worker writes an active payload. The Event is
                # visible immediately and its next save persists the flag.
                # A stop request must never overwrite newer worker progress.
                task["stop_requested"] = True
            return self._public(task)

    def prepare_exit(self) -> dict:
        with self._admission:
            if self._fatal_task and not (self._worker and self._worker.is_alive()):
                # The original durable journal is intentionally untouched.
                # No worker is doing I/O now; restart reconciles that journal.
                self._closing = True
                return {"safe": True, "task_id": None}
            active = self._active()
            if active or (self._worker and self._worker.is_alive()):
                latest = active or self._load()
                return {"safe": False, "task_id": latest["id"] if latest else None}
            self._closing = True
            return {"safe": True, "task_id": None}

    def close(self) -> None:
        with self._admission:
            self._closing = True
            self._stop.set()
        if self._worker and self._worker is not threading.current_thread():
            # Graceful shell exit has already waited for prepare_exit. In
            # tests/service shutdown, wait for the current safe boundary.
            self._worker.join()

    def _check_stop(self, task: dict) -> None:
        if self._stop.is_set() or task.get("stop_requested"):
            task["stop_requested"] = True
            raise _Stopped()

    def _checkpoint(self, stage: str, task: dict, item: dict) -> None:
        """Named transaction boundaries for failure-injection tests."""

    def _conflicts(self, target: Path, names: list[str]) -> list[str]:
        with os.scandir(target) as entries:
            present = {entry.name.casefold() for entry in entries}
        return [name for name in names if name.casefold() in present]

    def _preflight(self, task: dict) -> None:
        self._check_binding(task["_binding"])
        target = Path(task["target_path"])
        task["conflicts"] = self._conflicts(target, [item["name"] for item in task["items"]])
        if task["conflicts"]:
            raise _Conflict("存在同名目标，整批未开始，请先处理冲突", 409)
        for item in task["items"]:
            self._check_stop(task)
            source = Path(task["source_path"]) / item["name"]
            item["manifest"] = _manifest(source)
            item["size_bytes"] = sum(value["size"] for value in item["manifest"].values())
            for relative, entry in item["manifest"].items():
                if entry["kind"] == "file":
                    self._check_stop(task)
                    with _directory_guard((source / relative).parent), _open_read(source / relative) as stream:
                        if _fingerprint(os.fstat(stream.fileno())) != entry["fingerprint"]:
                            raise TransferError(f"源文件已变化：{relative}")
                        stream.read(1)  # detect unreadable/locked files in the whole queue
        task["total_bytes"] = sum(item["size_bytes"] for item in task["items"])
        if shutil.disk_usage(target).free < task["total_bytes"]:
            raise TransferError("NAS 可用空间不足，整批未开始")
        # Probe target write permission before copying anything. This exact
        # exclusive file is ours; no glob/rmtree cleanup is ever used.
        probe = target / f"{STAGING_PREFIX}{task['id']}.probe"
        with _directory_guard(target, task["_binding"]["target_identity"]):
            with probe.open("xb"):
                pass
            probe.unlink()
        self._save(task)

    def _copy(self, task: dict, item: dict) -> None:
        source = Path(task["source_path"]) / item["name"]
        staging = Path(item["staging_path"])
        manifest = item["manifest"]
        last_save = time.monotonic()
        for relative, entry in manifest.items():
            self._check_stop(task)
            self._check_binding(task["_binding"])
            _directory(source, manifest[""]["identity"])
            _directory(staging, item["staging_identity"])
            if entry["kind"] == "dir":
                if relative:
                    with _directory_guard((staging / relative).parent):
                        _directory(staging, item["staging_identity"])
                        (staging / relative).mkdir()
                continue
            original, copied = source / relative, staging / relative
            _directory(original.parent)
            if _fingerprint(_plain_stat(original)) != entry["fingerprint"]:
                raise TransferError(f"源文件已变化：{relative}")
            digest = hashlib.sha256()
            with (
                _directory_guard(original.parent), _directory_guard(copied.parent),
                _open_read(original) as inp, copied.open("xb") as out,
            ):
                self._check_binding(task["_binding"])
                _directory(source, manifest[""]["identity"])
                _directory(staging, item["staging_identity"])
                if _fingerprint(os.fstat(inp.fileno())) != entry["fingerprint"]:
                    raise TransferError(f"源文件已替换：{relative}")
                while chunk := inp.read(CHUNK_SIZE):
                    self._check_stop(task)
                    out.write(chunk)
                    digest.update(chunk)
                    item["progress_bytes"] += len(chunk)
                    task["progress_bytes"] += len(chunk)
                    if time.monotonic() - last_save >= 0.2:
                        self._save(task)
                        last_save = time.monotonic()
                out.flush()
                os.fsync(out.fileno())
                if _fingerprint(os.fstat(inp.fileno())) != entry["fingerprint"]:
                    raise TransferError(f"源文件在复制时发生变化：{relative}")
                os.utime(copied, ns=(entry["atime_ns"], entry["mtime_ns"]))
            entry["sha256"] = digest.hexdigest()
        self._save(task)

    def _verify(self, task: dict, item: dict, root: Path, *, source: bool = False, stopping: bool = True) -> None:
        _check_manifest(root, item["manifest"], identities=source)
        last_save = time.monotonic()
        def progress(count):
            nonlocal last_save
            if stopping:
                self._check_stop(task)
            if not source:
                task["verified_bytes"] += count
                if time.monotonic() - last_save >= 0.2:
                    self._save(task)
                    last_save = time.monotonic()
        for relative, entry in item["manifest"].items():
            if entry["kind"] == "file":
                _directory((root / relative).parent)
                with _directory_guard((root / relative).parent), _open_read(root / relative) as stream:
                    if _hash(stream, progress) != entry["sha256"]:
                        raise TransferError(f"SHA-256 内容校验失败：{relative}")

    def _cleanup(self, task: dict, item: dict) -> None:
        source = Path(task["source_path"]) / item["name"]
        target = Path(task["target_path"]) / item["name"]
        manifest = item["manifest"]
        self._check_binding(task["_binding"])
        _directory(target, item["staging_identity"])
        _check_manifest(target, manifest, identities=False)
        # A complete recheck detects new files before *any* source deletion.
        _check_manifest(source, manifest, identities=True)
        for relative, entry in manifest.items():
            if entry["kind"] != "file":
                continue
            self._check_binding(task["_binding"])
            _directory(source, manifest[""]["identity"])
            _directory(target, item["staging_identity"])
            parent = Path(relative).parent.as_posix()
            parent_identity = manifest["" if parent == "." else parent]["identity"]
            _directory((source / relative).parent, parent_identity)
            _directory((target / relative).parent)
            with _directory_guard((source / relative).parent), _directory_guard((target / relative).parent):
                self._check_binding(task["_binding"])
                _directory(source, manifest[""]["identity"])
                _directory(target, item["staging_identity"])
                _delete_exact(source / relative, target / relative, entry)
        directories = [(rel, entry) for rel, entry in manifest.items() if entry["kind"] == "dir"]
        for relative, entry in sorted(directories, key=lambda pair: len(Path(pair[0]).parts), reverse=True):
            self._check_binding(task["_binding"])
            with _directory_guard((source / relative).parent):
                _directory(source / relative, entry["identity"])
                if sys.platform == "win32":
                    _windows_directory_change(source / relative, entry["identity"])
                else:
                    (source / relative).rmdir()  # fails safely when anything new appeared

    def _move_item(self, task: dict, item: dict) -> None:
        self._check_stop(task)
        self._check_binding(task["_binding"])
        task["current_name"] = item["name"]
        target = Path(task["target_path"])
        task["conflicts"] = self._conflicts(target, [item["name"]])
        if task["conflicts"]:
            raise _Conflict("传输期间出现同名目标，已停止本次队列", 409)
        staging = target / f"{STAGING_PREFIX}{task['id']}-{uuid.uuid4().hex}"
        item["staging_path"] = str(staging)
        item["status"] = task["phase"] = "copying"
        item["message"] = task["message"] = "正在复制完整影片文件夹"
        # Record the intended path before creating it. If power fails between
        # mkdir and recording its identity, recovery will not touch it.
        self._save(task)
        with _directory_guard(target, task["_binding"]["target_identity"]):
            staging.mkdir()
        item["staging_identity"] = _directory(staging)
        self._save(task)
        self._checkpoint("before_copy", task, item)
        self._copy(task, item)
        self._checkpoint("after_copy", task, item)
        item["status"] = task["phase"] = "verifying"
        item["message"] = task["message"] = "正在读取目标并校验 SHA-256"
        self._save(task)
        self._verify(task, item, staging)
        self._verify(task, item, Path(task["source_path"]) / item["name"], source=True)
        self._checkpoint("after_verify", task, item)
        self._check_stop(task)
        self._check_binding(task["_binding"])
        item["status"] = task["phase"] = "committing"
        item["message"] = task["message"] = "校验通过，正在提交目标目录"
        self._save(task)
        self._checkpoint("before_commit", task, item)
        task["conflicts"] = self._conflicts(target, [item["name"]])
        if task["conflicts"]:
            raise _Conflict("传输期间出现同名目标，已停止本次队列", 409)
        _directory(staging, item["staging_identity"])
        try:
            with _directory_guard(target, task["_binding"]["target_identity"], committing=True):
                _directory(staging, item["staging_identity"])
                _rename_no_replace(staging, target / item["name"], item["staging_identity"])
        except FileExistsError:
            task["conflicts"] = [item["name"]]
            raise _Conflict("提交时出现同名目标，未覆盖，已停止队列", 409) from None
        # Keep the committing journal until cleaning has been durably written.
        # A process crash here is reconciled using the staged directory ID.
        item["committed"] = True
        self._checkpoint("after_commit", task, item)
        item["status"] = task["phase"] = "cleaning"
        item["message"] = task["message"] = "目标已完整提交，正在核对并清理本地源"
        self._save(task)
        self._cleanup(task, item)
        self._checkpoint("after_cleanup", task, item)
        item["status"] = "completed"
        item["message"] = "影片已移动，目标完整且本地源已清理"
        task["completed_count"] += 1
        self._save(task)

    @staticmethod
    def _error_message(exc: Exception) -> str:
        message = str(exc) or type(exc).__name__
        if isinstance(exc, OSError) and getattr(exc, "winerror", None) in {3, 206}:
            if len(str(exc.filename or "")) >= 248:
                message = "路径过长或当前 Windows／共享不支持该路径长度；" + message
        return message

    def _journal_failed(self, exc: Exception) -> None:
        # In-memory only: writing another failure record to the same broken
        # journal could fail again and strand a forever-active job. Keeping
        # the last durable phase lets the next launch reconcile filesystem
        # state without pretending this report itself was persisted.
        with self._admission:
            task = copy.deepcopy(self._working_task)
            task["status"] = "needs_review"
            task["phase"] = "finished"
            task["message"] = "任务状态未能保存到数据库，传输已停止。请重新启动应用核对结果；" + self._error_message(exc)
            for item in task["items"]:
                if item["status"] in {"copying", "verifying", "committing", "cleaning"}:
                    item["status"] = "needs_review"
                    item["message"] = task["message"]
            self._fatal_task = task

    def _run(self, task_id: str) -> None:
        try:
            self._execute(task_id)
        except Exception as exc:
            self._journal_failed(exc)

    def _execute(self, task_id: str) -> None:
        task = self._load(task_id)
        if task is None:
            return
        current = None
        try:
            self._check_stop(task)
            task["status"] = "running"
            self._save(task)
            self._preflight(task)
            for current in task["items"]:
                self._move_item(task, current)
            task["status"] = "completed"
            task["message"] = "所选影片已全部移动完成"
        except _Stopped:
            task["status"] = "stopped"
            task["message"] = "已停止传输，未提交的源影片保留"
            if current and current["status"] != "completed":
                current["status"] = "stopped"
                current["message"] = task["message"]
        except Exception as exc:
            committed = current and (current.get("committed") or current["status"] == "cleaning")
            ambiguous = current and current["status"] == "committing" and not isinstance(exc, _Conflict)
            task["status"] = "needs_review" if committed or ambiguous else "failed"
            task["message"] = self._error_message(exc)
            if current:
                current["status"] = "cleanup_pending" if committed else "needs_review" if ambiguous else "failed"
                current["message"] = task["message"]
        if current and current.get("staging_path") and current["status"] in {"failed", "stopped", "needs_review"}:
            current["message"] += f"；本任务暂存位置（可能有未完成内容）：{current['staging_path']}"
        task["phase"] = "finished"
        task["stop_requested"] = self._stop.is_set() or task["stop_requested"]
        self._save(task)

    def recover(self) -> None:
        """Reconcile interrupted records, never resume or delete anything."""
        with self._admission:
            if self._worker and self._worker.is_alive():
                return
            with self.db_lock:
                rows = self.conn.execute("SELECT payload FROM archive_tasks ORDER BY rowid").fetchall()
            pending = [json.loads(row["payload"]) for row in rows]
            pending = [task for task in pending if task["status"] in ACTIVE]
            if not pending:
                return
            if self._auto_start:
                self._worker = threading.Thread(target=self._recover, args=(pending,), daemon=True, name="archive-recovery")
                self._worker.start()
            else:
                self._recover(pending)

    def _recover(self, tasks: list[dict]) -> None:
        try:
            self._reconcile(tasks)
        except Exception as exc:
            self._journal_failed(exc)

    def _reconcile(self, tasks: list[dict]) -> None:
        for task in tasks:
            self._working_task = task
            task["status"] = "running"
            task["phase"] = "verifying"
            task["verified_bytes"] = sum(item["size_bytes"] for item in task["items"] if item["status"] == "completed")
            task["message"] = "正在核对上次中断的传输，不会自动移动或删除"
            self._save(task)
            for item in task["items"]:
                if item["status"] in {"completed", "pending"}:
                    continue
                if item["status"] not in {"committing", "cleaning"}:
                    item["status"] = "failed"
                    item["message"] = "上次复制或校验中断，源文件保留；暂存残留未自动清理"
                    if item.get("staging_path"):
                        item["message"] += f"；暂存位置：{item['staging_path']}"
                    continue
                try:
                    self._check_binding(task["_binding"])
                    destination = Path(task["target_path"]) / item["name"]
                    _directory(destination, item["staging_identity"])
                    self._verify(task, item, destination)
                    original = Path(task["source_path"]) / item["name"]
                    try:
                        original.lstat()
                    except FileNotFoundError:
                        item["status"] = "completed"
                        item["message"] = "已核对：目标完整且源目录已清理"
                    else:
                        item["status"] = "cleanup_pending"
                        item["message"] = "目标已完整提交，本地源仍有残留，请手动核对"
                except Exception as exc:
                    item["status"] = "needs_review"
                    item["message"] = f"上次传输中断，无法确认提交结果，请手动核对：{exc}"
            task["completed_count"] = sum(item["status"] == "completed" for item in task["items"])
            task["status"] = "completed" if task["completed_count"] == task["total_count"] else "needs_review"
            task["phase"] = "finished"
            task["message"] = "上次传输结果已核对" if task["status"] == "completed" else "上次传输中断，未自动继续或删除，请核对逐项结果"
            self._save(task)
