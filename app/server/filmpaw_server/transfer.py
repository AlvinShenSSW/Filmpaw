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
import re
import shutil
import sqlite3
import stat
import struct
import sys
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from filmpaw_server.directory_reads import directory_error_retryable
from filmpaw_server.audit import AuditError, OperationLog
from filmpaw_server import path_resources


CHUNK_SIZE = 1024 * 1024
STAGING_PREFIX = ".filmpaw-transfer-"
ACTIVE = {"queued", "running"}
CONCURRENCY = 3
QUEUE_LIMIT = 100
# Workflow archive: the approval's target/package proof no longer holds, so waiting cannot help and
# Taskpaw records the archive as failed (409/503 remain "busy, try again later").
STALE = 412


class TransferError(Exception):
    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.status_code = status_code


class NamedStreamsError(TransferError):
    """Content exists outside the unnamed stream; callers must preserve it."""


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
        yield handles[-1]
    finally:
        for handle in reversed(handles):
            close(handle)


def _check_named_streams(handle, path: Path) -> None:
    """Reject content the default-stream copier cannot preserve, by handle.

    Windows sharing is per stream: even a default-stream DELETE handle cannot
    atomically exclude a new named stream. Recheck immediately before deleting,
    but concurrent external modifications must still stop during a transfer.
    """
    if sys.platform != "win32":
        return
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    get_info = kernel.GetFileInformationByHandleEx
    get_info.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    get_info.restype = wintypes.BOOL
    size = 4096
    while True:
        buffer = ctypes.create_string_buffer(size)
        if get_info(handle, 7, buffer, size):  # FileStreamInfo
            break
        error = ctypes.get_last_error()
        if error == 38:  # ERROR_HANDLE_EOF: no streams (e.g. a plain directory)
            return
        if error in {122, 234} and size < 1024 * 1024:
            size *= 2
            continue
        raise TransferError(f"无法核验额外数据流，保留本地源：{path}；{ctypes.WinError(error)}")
    offset = 0
    while True:
        # FILE_STREAM_INFO has a 24-byte header followed by a non-terminated
        # UTF-16 name. A malformed or oversized response must fail closed.
        if offset + 24 > size:
            raise TransferError(f"额外数据流信息无效，保留本地源：{path}")
        next_offset, name_size = struct.unpack_from("<II", buffer, offset)
        end = offset + 24 + name_size
        if not name_size or name_size % 2 or end > size:
            raise TransferError(f"额外数据流信息无效，保留本地源：{path}")
        name = buffer.raw[offset + 24:end].decode("utf-16-le", errors="surrogatepass")
        if name != "::$DATA":
            raise NamedStreamsError(f"不支持额外数据流（ADS），保留本地源：{path} {name}")
        if not next_offset:
            return
        if next_offset < 24 + name_size or next_offset % 8:
            raise TransferError(f"额外数据流信息无效，保留本地源：{path}")
        offset += next_offset


def _check_file_streams(stream, path: Path) -> None:
    if sys.platform == "win32":
        import msvcrt
        _check_named_streams(msvcrt.get_osfhandle(stream.fileno()), path)


@contextmanager
def _open_read(path: Path, *, deleting: bool = False):
    """Windows denies writes to this default stream and file rename/delete.

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
            _check_named_streams(handle, path)
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
            _check_file_streams(source_stream, path)
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
        self.audit = OperationLog(conn, db_lock)
        self._runtimes = {}
        self._queue = []
        self._dispatcher = None
        self._wake = threading.Event()
        self._recovery_worker = None
        self._recovering = False
        self._recovery_tasks = {}
        self._closing = False
        self._auto_start = auto_start
        self._storage_error = None
        self._fatal_tasks = {}
        self._context_read_lock = threading.Lock()
        self._cleanup_id = None

    def ensure_open(self) -> None:
        with self._admission:
            if self._closing:
                raise TransferError("应用正在退出，不能开始新操作", 409)
            if self._storage_error:
                raise TransferError(self._storage_error, 409)

    def reserve_cleanup(self, operation_id: str, batch_path: str, batch_revision: str) -> None:
        """Atomically bind a cleanup to the current settings and write slot."""
        with self._admission, self.db_lock:
            self.ensure_open()
            if self._cleanup_id or self._busy_locked():
                raise TransferError("已有传输或清理任务正在进行", 409)
            settings = dict(self.conn.execute(
                "SELECT key,value FROM settings WHERE key IN ('last_local_dir','last_local_dir_revision')"
            ).fetchall())
            if (settings.get("last_local_dir") != batch_path
                    or (settings.get("last_local_dir_revision") or "legacy") != batch_revision):
                raise TransferError("batch 目录选择已变化，请刷新后重新清理", 409)
            self._cleanup_id = operation_id

    def release_cleanup(self, operation_id: str) -> None:
        with self._admission:
            if self._cleanup_id == operation_id:
                self._cleanup_id = None

    def begin_close(self) -> None:
        with self._admission:
            self._closing = True

    def _read_binding(self, local_dir: str, subdir: str, performer_id: str) -> dict:
        _name(subdir)
        with self.db_lock:
            setting = self.conn.execute("SELECT value FROM settings WHERE key='last_local_dir'").fetchone()
            revision = self.conn.execute(
                "SELECT value FROM settings WHERE key='last_local_dir_revision'"
            ).fetchone()
        if setting is None or not setting["value"] or setting["value"] != local_dir:
            raise TransferError("本地 batch 目录已变化，请重新选择目录", 409)
        binding = self._performer_binding(local_dir, subdir, performer_id)
        binding["_batch_revision"] = revision["value"] if revision else ""
        return binding

    def _performer(self, performer_id: str):
        with self.db_lock:
            performer = self.conn.execute(
                "SELECT p.unc_path,p.name,p.is_missing,p.thumb IS NOT NULL AS has_thumb,s.label AS source_label"
                " FROM performers p JOIN sources s ON s.id=p.source_id WHERE p.id=?", (performer_id,)
            ).fetchone()
        if performer is None:
            raise TransferError("所选演员记录已不存在", 404)
        if performer["is_missing"]:
            raise TransferError("所选演员目录已失效，请刷新片库", 409)
        return performer

    def _performer_binding(self, local_dir: str, subdir: str, performer_id: str) -> dict:
        _name(subdir)
        performer = self._performer(performer_id)
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
        }

    def _current_binding(self, binding: dict) -> dict:
        if binding.get("_workflow"):
            current = self._performer_binding(binding["local_dir"], binding["subdir"], binding["performer_id"])
            current["_batch_revision"] = binding["_batch_revision"]
            return current
        return self._read_binding(binding["local_dir"], binding["subdir"], binding["performer_id"])

    # ── workflow archive (Taskpaw F02-P1): copy one approved package, keep the source ──────────
    def workflow_target(self, performer_id: str, film_dir: str) -> dict:
        """Target proof: the performer folder identity and whether <film_dir> is still free."""
        film = _name(film_dir)
        target = Path(self._performer(performer_id)["unc_path"]).absolute()
        try:
            identity = _directory(target)
            with os.scandir(target) as entries:
                present = {e.name.casefold() for e in entries if not e.name.startswith(STAGING_PREFIX)}
        except OSError as exc:  # NAS asleep / offline: try later; folder gone: stale
            status = 503 if directory_error_retryable(exc) else STALE
            raise TransferError(f"无法读取 NAS 演员目录：{exc}", status) from exc
        return {
            "performer_id": performer_id,
            "absent": film.casefold() not in present,
            "expected_target": {"target_path": str(target), "directory_identity": identity},
        }

    @staticmethod
    def _workflow_files(files: list[dict]) -> dict:
        expected = {}
        for entry in files:
            relative, size, sha = entry.get("path"), entry.get("size"), entry.get("sha256")
            parts = relative.split("/") if isinstance(relative, str) else []
            if not parts or "\\" in relative or any(part in ("", ".", "..") for part in parts):
                raise TransferError(f"清单路径无效：{relative!r}")
            for part in parts:
                _name(part)
            if relative in expected:
                raise TransferError(f"清单路径重复：{relative}")
            if type(size) is not int or size < 0 or not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{64}", sha):
                raise TransferError(f"清单条目无效：{relative}")
            expected[relative] = [size, sha]
        if not expected:
            raise TransferError("清单为空")
        return expected

    def create_workflow_task(
        self, operation_id: str, package_dir: str, performer_id: str, expected_target: dict, files: list[dict]
    ) -> dict:
        if not operation_id or len(operation_id) > 128:
            raise TransferError("无效的操作键")
        package = Path(package_dir)
        if not package.is_absolute():
            raise TransferError("影片包路径必须是绝对路径")
        film = _name(package.name)
        target_path = expected_target.get("target_path")
        identity = expected_target.get("directory_identity")
        if not isinstance(target_path, str) or not isinstance(identity, list):
            raise TransferError("缺少审批时的目标证明")
        workflow = {
            "keep_source": True, "expected": self._workflow_files(files), "package_dir": str(package),
            "performer_id": performer_id,
            "expected_target": {"target_path": target_path, "directory_identity": list(identity)},
        }
        with self._admission:
            previous = self._load(request_id=operation_id)
        if previous:
            if previous["_binding"].get("_workflow") != workflow:
                raise TransferError("操作键已用于另一个归档请求", STALE)
            return self.get_by_request(operation_id)
        try:
            binding = self._performer_binding(str(package.parent.parent), package.parent.name, performer_id)
        except TransferError as exc:
            if exc.status_code == 409:  # performer folder gone / nested: will not clear by waiting
                raise TransferError(str(exc), STALE) from None
            raise
        if binding["target_path"] != target_path:
            raise TransferError("NAS 演员目录已变化，请重新确认入库", STALE)
        binding["_workflow"] = workflow
        binding["_batch_revision"] = ""
        if not self._context_read_lock.acquire(blocking=False):
            raise TransferError("目录仍在读取，请稍候重试", 503)
        try:
            context = self._create_context(binding)
        finally:
            self._context_read_lock.release()
        if context["target_error"] is None and binding.get("target_identity") != workflow["expected_target"]["directory_identity"]:
            raise TransferError("NAS 演员目录身份已变化，请重新确认入库", STALE)
        if context["source_error"] or film not in {i["name"] for i in context["source_items"] if i["error"] is None}:
            raise TransferError(f"影片包不可读：{context['source_error'] or film}", STALE)
        if context["target_error"]:  # NAS offline / slow: try later; folder gone / replaced: stale
            status = 503 if context["target_retryable"] else STALE
            raise TransferError(f"NAS 演员目录不可用：{context['target_error']}", status)
        return self.submit(context["id"], [film], operation_id)

    def get_workflow_task(self, operation_id: str) -> dict | None:
        return self.get_by_request(operation_id)

    @staticmethod
    def _check_workflow_files(item: dict, workflow: dict, *, hashes: bool) -> None:
        files = {rel: entry for rel, entry in item["manifest"].items() if entry["kind"] == "file"}
        expected = workflow["expected"]
        if files.keys() != expected.keys():
            extra = sorted(files.keys() - expected.keys())[:5]
            missing = sorted(expected.keys() - files.keys())[:5]
            raise TransferError(f"影片包内容与已批准清单不一致：多出 {extra}，缺少 {missing}")
        for relative, entry in files.items():
            size, sha = expected[relative]
            if entry["size"] != size or (hashes and entry.get("sha256") != sha):
                raise TransferError(f"影片包文件与已批准清单不一致：{relative}")

    @staticmethod
    def _workflow_receipt(task: dict, item: dict) -> dict:
        workflow = task["_binding"]["_workflow"]
        files = [
            {"path": rel, "size": entry["size"], "sha256": entry["sha256"]}
            for rel, entry in item["manifest"].items() if entry["kind"] == "file"
        ]
        return {
            "task_id": task["id"],
            "target_path": str(Path(workflow["expected_target"]["target_path"]) / item["name"]),
            "directory_identity": item["staging_identity"],
            "files": sorted(files, key=lambda f: f["path"]),
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
                resource = path_resources.capture(path)
                binding.setdefault("_resources", []).append(resource)
                binding[f"_original_{side}_path"] = str(path)
                binding[f"{side}_path"] = resource["path"]
                # Freeze mapped drive aliases before a request can be queued.
                if side == "source":
                    binding["_batch_path"] = str(Path(resource["path"]).parent)
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
        _directory(Path(binding.get("_batch_path", binding["local_dir"])), binding["batch_identity"])
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
        runtime = self._runtimes.get(task["id"])
        task["updated_at"] = _now()
        if runtime and runtime["stop"].is_set() and task["status"] in ACTIVE:
            task["stop_requested"] = True
        public = self._public(task)
        if runtime is not None:
            # Publish an independent small snapshot. Another worker's storage
            # failure must not copy this worker's mutating manifest dictionary.
            runtime["snapshot"] = public
        self._check_storage()
        with self.db_lock:
            try:
                self.conn.execute(
                    "INSERT INTO archive_tasks(id,request_id,payload,public_payload) VALUES (?,?,?,?)"
                    " ON CONFLICT(id) DO UPDATE SET payload=excluded.payload,public_payload=excluded.public_payload",
                    (task["id"], task["request_id"], json.dumps(task, ensure_ascii=False),
                     json.dumps(public, ensure_ascii=False)),
                )
                self.conn.commit()
            except Exception as exc:
                try:
                    self.conn.rollback()
                except Exception:
                    pass
                self._storage_error = self._storage_error or f"任务状态未能保存：{exc}；请重启后核对结果"
                raise AuditError(self._storage_error) from exc

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
            if task_id in self._fatal_tasks:
                return self._public(self._fatal_tasks[task_id])
            if task_id is None and self._fatal_tasks:
                return self._public(next(reversed(self._fatal_tasks.values())))
        task = self._load(task_id)
        if task_id is not None and task is None:
            raise TransferError("传输任务不存在", 404)
        return self._visible(task) if task else None

    def _visible(self, task: dict) -> dict:
        task = self._fatal_tasks.get(task["id"], task)
        result = self._public(task)
        runtime = self._runtimes.get(task["id"])
        if runtime and runtime["stop"].is_set():
            result["stop_requested"] = True
        return result

    def get_by_request(self, request_id: str) -> dict | None:
        with self._admission:
            task = self._load(request_id=request_id)
            return self._visible(task) if task else None

    def _reap_locked(self) -> None:
        for task_id, runtime in list(self._runtimes.items()):
            worker = runtime["worker"]
            if runtime["finished"] and (worker is None or not worker.is_alive()):
                del self._runtimes[task_id]
        if not self._recovering and not (self._recovery_worker and self._recovery_worker.is_alive()):
            self._recovery_tasks.clear()

    def _busy_locked(self) -> bool:
        self._reap_locked()
        return bool(self._runtimes or self._recovering
                    or (self._recovery_worker and self._recovery_worker.is_alive()) or self._orphan_ids())

    def _orphan_ids(self) -> list[str]:
        # A durable interrupted job is never idle merely because this process
        # has not reconstructed a worker for it yet.
        with self.db_lock:
            rows = self.conn.execute("SELECT id FROM archive_tasks WHERE"
                " json_extract(coalesce(public_payload,payload),'$.status') IN ('queued','running')").fetchall()
        return [row["id"] for row in rows if row["id"] not in self._runtimes
                and row["id"] not in self._fatal_tasks]

    def list_tasks(self, *, before_id: int | None = None, limit: int = 50) -> dict:
        if not 1 <= limit <= 200 or (before_id is not None and before_id < 1):
            raise TransferError("无效的任务分页参数")
        with self._admission:
            self._reap_locked()
            orphans = self._orphan_ids()
            recovering = self._recovering or bool(self._recovery_worker and self._recovery_worker.is_alive())
            busy = bool(self._runtimes or orphans or recovering)
            active_ids = list(dict.fromkeys([*self._runtimes, *orphans]))
            if recovering:
                active_ids = list(dict.fromkeys([*active_ids, *self._recovery_tasks]))
            with self.db_lock:
                slots = ",".join("?" for _ in active_ids)
                active_rows = self.conn.execute(
                    f"SELECT public_payload FROM archive_tasks WHERE id IN ({slots}) ORDER BY rowid DESC", active_ids,
                ).fetchall() if active_ids else []
                clauses, params = [], []
                if active_ids:
                    clauses.append(f"id NOT IN ({slots})")
                    params.extend(active_ids)
                if before_id is not None:
                    clauses.append("rowid<?")
                    params.append(before_id)
                where = " WHERE " + " AND ".join(clauses) if clauses else ""
                rows = self.conn.execute("SELECT rowid,public_payload FROM archive_tasks" + where +
                    " ORDER BY rowid DESC LIMIT ?", [*params, limit + 1]).fetchall()
            active = [self._visible(json.loads(row["public_payload"])) for row in active_rows]
            history = [(row["rowid"], self._visible(json.loads(row["public_payload"]))) for row in rows]
            blocked = self._storage_error or ("正在核对上次中断的任务" if recovering or orphans else None)
            if self._closing:
                blocked = blocked or "应用正在退出"
            return {"active": active, "history": [task for _, task in history[:limit]],
                    "next_cursor": history[limit - 1][0] if len(history) > limit else None,
                    "busy": busy, "blocked_reason": blocked, "concurrency": CONCURRENCY}

    def submit(self, context_id: str, names: list[str], request_id: str) -> dict:
        if not request_id or len(request_id) > 128:
            raise TransferError("无效的幂等请求标识")
        if not names or len(names) > 1000:
            raise TransferError("请选择 1 至 1000 部影片")
        names = [_name(name) for name in names]
        if len({name.casefold() for name in names}) != len(names):
            raise TransferError("影片选择包含重复名称")
        with self._admission:
            previous = self._load(request_id=request_id)
            if previous:
                if previous["context_id"] != context_id or [i["name"] for i in previous["items"]] != names:
                    raise TransferError("幂等标识已用于另一组传输选择", 409)
                return self._visible(previous)
            self.ensure_open()
            if self._cleanup_id:
                raise TransferError("正在清理空演员目录，请完成后再传输", 409)
            if self._recovering or (self._recovery_worker and self._recovery_worker.is_alive()) or self._orphan_ids():
                raise TransferError("正在核对上次中断的传输，请稍候", 409)
            self._reap_locked()
            if len(self._runtimes) >= QUEUE_LIMIT:
                raise TransferError("传输队列已满（最多 100 个任务），请稍候", 409)
            context = self._contexts.get(context_id)
            if context is None:
                raise TransferError("归档上下文已失效，请刷新列表", 409)
            public, binding = context["public"], context["binding"]
            if public["source_error"] or public["target_error"]:
                raise TransferError("源或目标目录不可用，请刷新后重试", 409)
            current = self._current_binding(binding)
            if current["_batch_revision"] != binding["_batch_revision"]:
                raise TransferError("batch 目录选择已变化，原归档上下文失效，请刷新列表", 409)
            if current["target_path"] != binding.get("_original_target_path", binding["target_path"]):
                raise TransferError("NAS 演员目录已变化，请重新选择目标", 409)
            resources = binding["_resources"]
            if path_resources.overlaps(*resources):
                raise TransferError("本地源与 NAS 目标不能相同或互相嵌套", 409)
            if any(path_resources.overlaps(a, b) for runtime in self._runtimes.values()
                   for a in resources for b in runtime["resources"]):
                raise TransferError("演员源或目标目录已被其他任务占用，请等待该任务结束", 409)
            available = {item["name"] for item in public["source_items"] if item["error"] is None}
            if not set(names).issubset(available):
                raise TransferError("影片不属于当前列表，请刷新后重新选择", 409)
            task = {
                "id": str(uuid.uuid4()), "request_id": request_id, "context_id": context_id,
                **{k: v for k, v in binding.items() if not k.endswith("_identity") and not k.startswith("_")},
                "status": "queued", "phase": "queued", "current_name": None,
                "progress_bytes": 0, "total_bytes": 0, "verified_bytes": 0,
                "completed_count": 0, "total_count": len(names), "conflicts": [],
                "message": "等待传输名额", "stop_requested": False,
                "created_at": _now(), "updated_at": _now(), "_binding": copy.deepcopy(binding),
                "items": [{"name": name, "status": "pending", "size_bytes": 0,
                           "progress_bytes": 0, "message": "等待预检"} for name in names],
            }
            self._runtimes[task["id"]] = {"stop": threading.Event(), "worker": None,
                "snapshot": self._public(task), "finished": False, "started": False, "resources": resources}
            try:
                self._save(task)
                self.audit.record(task, "transfer_accepted", "succeeded")
                self._queue.append(task["id"])
                if self._auto_start:
                    self._start_dispatcher_locked()
            except Exception as exc:
                self.storage_failed(self._error_message(exc), task["id"])
                raise TransferError(self._storage_error, 503) from exc
            return self._public(task)

    def _start_dispatcher_locked(self) -> None:
        if self._dispatcher is None or not self._dispatcher.is_alive():
            thread = threading.Thread(target=self._dispatch, daemon=True, name="archive-scheduler")
            thread.start()
            self._dispatcher = thread
        self._wake.set()

    def _dispatch(self) -> None:
        while True:
            with self._admission:
                self._reap_locked()
                if self._closing and not self._runtimes:
                    return
                if not self._closing and not self._storage_error and not self._recovering:
                    running = sum(runtime["worker"] is not None for runtime in self._runtimes.values())
                    while self._queue and running < CONCURRENCY:
                        task_id = self._queue.pop(0)
                        runtime = self._runtimes[task_id]
                        worker = threading.Thread(target=self._run, args=(task_id,), daemon=True,
                                                  name=f"archive-{task_id[:8]}")
                        runtime["worker"] = worker
                        try:
                            worker.start()
                        except Exception as exc:
                            runtime["worker"] = None
                            try:
                                task = self._load(task_id)
                                task.update(status="failed", phase="finished", message=f"无法启动传输：{exc}")
                                self._save(task)
                                self.audit.record(task, "transfer_finished", "failed", message=task["message"])
                            except Exception as failure:
                                self.storage_failed(str(failure), task_id)
                                break
                            runtime["finished"] = True
                            self._reap_locked()
                        else:
                            running += 1
            # A worker signals before Python marks its Thread stopped. Recheck
            # boundedly so ownership survives the entire finalization window.
            self._wake.wait(0.05)
            self._wake.clear()

    def stop(self, task_id: str) -> dict:
        with self._admission:
            if task_id in self._fatal_tasks:
                return self._public(self._fatal_tasks[task_id])
            task = self._load(task_id)
            if task is None:
                raise TransferError("传输任务不存在", 404)
            if task["status"] in ACTIVE:
                runtime = self._runtimes.get(task_id)
                if runtime is None or self._recovering:
                    raise TransferError("正在核对中断任务，请等待核对结束", 409)
                try:
                    self.audit.record(task, "stop_requested", "succeeded")
                    runtime["stop"].set()
                    if not runtime["started"] and runtime["worker"] is None:
                        # Re-read under admission: a stale caller snapshot may
                        # never overwrite a newer terminal journal.
                        task = self._load(task_id)
                        if task["status"] in ACTIVE:
                            task.update(status="stopped", phase="finished", stop_requested=True,
                                        message="等待中的任务已取消，未移动任何影片")
                            self._save(task)
                            self.audit.record(task, "transfer_finished", "stopped", message=task["message"])
                        if task_id in self._queue:
                            self._queue.remove(task_id)
                        runtime["finished"] = True
                        self._reap_locked()
                except Exception as exc:
                    self.storage_failed(str(exc), task_id)
                    return self._public(self._fatal_tasks[task_id])
                # Only the worker writes an active payload. The Event is
                # visible immediately and its next save persists the flag.
                # A stop request must never overwrite newer worker progress.
                task["stop_requested"] = True
            return self._public(task)

    def stop_all(self, task_ids: list[str]) -> dict:
        ids = list(dict.fromkeys(task_ids))
        if len(ids) > QUEUE_LIMIT:
            raise TransferError("一次最多停止 100 个任务")
        with self._admission:
            if any(self._load(task_id) is None for task_id in ids):
                raise TransferError("传输任务不存在", 404)
            for task_id in ids:
                self.stop(task_id)
        return {"task_ids": ids}

    def prepare_exit(self) -> dict:
        with self._admission:
            if self._cleanup_id:
                return {"safe": False, "task_id": self._cleanup_id, "operation": "cleanup"}
            if self._busy_locked():
                task_id = (next(iter(self._runtimes), None) or next(iter(self._recovery_tasks), None)
                           or next(iter(self._orphan_ids()), None))
                return {"safe": False, "task_id": task_id}
            self._closing = True
            return {"safe": True, "task_id": None}

    def close(self) -> None:
        with self._admission:
            self._closing = True
            for task_id in list(self._queue):
                self.stop(task_id)
            for runtime in self._runtimes.values():
                runtime["stop"].set()
            workers = [r["worker"] for r in self._runtimes.values() if r["worker"] is not None]
            workers += [self._recovery_worker, self._dispatcher]
            self._wake.set()
        for worker in workers:
            if worker is not None and worker is not threading.current_thread():
                worker.join()

    def _check_stop(self, task: dict) -> None:
        self._check_storage()
        runtime = self._runtimes.get(task["id"])
        if (runtime and runtime["stop"].is_set()) or task.get("stop_requested"):
            task["stop_requested"] = True
            raise _Stopped()

    def _check_storage(self) -> None:
        if self._storage_error or self.audit.failed:
            raise AuditError(self._storage_error or "操作日志不可写，后续操作已停止")

    def _perform(self, task: dict, action: str, fn, **fields):
        def checked_action():
            self._check_storage()
            return fn()
        self._check_storage()
        return self.audit.perform(task, action, checked_action, **fields)

    def _checkpoint(self, stage: str, task: dict, item: dict) -> None:
        """Named transaction boundaries for failure-injection tests."""

    def _conflicts(self, target: Path, names: list[str]) -> list[str]:
        with os.scandir(target) as entries:
            present = {entry.name.casefold() for entry in entries}
        return [name for name in names if name.casefold() in present]

    def _check_source_metadata(self, task: dict, item: dict, *, stopping: bool = True) -> None:
        """Check every selected file and directory before moving or cleaning."""
        source = Path(task["source_path"]) / item["name"]
        for relative, entry in item["manifest"].items():
            if stopping:
                self._check_stop(task)
            path = source / relative
            if entry["kind"] == "dir":
                with _directory_guard(path, entry["identity"]) as handle:
                    _check_named_streams(handle, path)
            else:
                with _directory_guard(path.parent), _open_read(path) as stream:
                    if _fingerprint(os.fstat(stream.fileno())) != entry["fingerprint"]:
                        raise TransferError(f"源文件已变化：{relative}")
                    _check_file_streams(stream, path)
                    stream.read(1)  # detect unreadable/locked files in the whole queue

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
            self._check_source_metadata(task, item)
            if task["_binding"].get("_workflow"):
                self._check_workflow_files(item, task["_binding"]["_workflow"], hashes=False)
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
                    if source:
                        _check_file_streams(stream, root / relative)
            elif source:
                with _directory_guard(root / relative, entry["identity"]) as handle:
                    _check_named_streams(handle, root / relative)

    def _cleanup(self, task: dict, item: dict) -> None:
        source = Path(task["source_path"]) / item["name"]
        target = Path(task["target_path"]) / item["name"]
        manifest = item["manifest"]
        self._check_binding(task["_binding"])
        _directory(target, item["staging_identity"])
        _check_manifest(target, manifest, identities=False)
        # A complete recheck detects new files before *any* source deletion.
        _check_manifest(source, manifest, identities=True)
        self._check_source_metadata(task, item, stopping=False)
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
                self._check_storage()
                self._perform(task, "delete_source_file",
                    lambda: _delete_exact(source / relative, target / relative, entry),
                    item_name=item["name"], source_path=str(source / relative), target_path=str(target / relative))
        directories = [(rel, entry) for rel, entry in manifest.items() if entry["kind"] == "dir"]
        for relative, entry in sorted(directories, key=lambda pair: len(Path(pair[0]).parts), reverse=True):
            self._check_binding(task["_binding"])
            with _directory_guard((source / relative).parent):
                _directory(source / relative, entry["identity"])
                self._check_storage()
                def remove_directory():
                    if sys.platform == "win32":
                        _windows_directory_change(source / relative, entry["identity"])
                    else:
                        (source / relative).rmdir()  # fails safely when anything new appeared
                self._perform(task, "delete_source_directory", remove_directory,
                    item_name=item["name"], source_path=str(source / relative), target_path=str(target / relative))

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
        self._perform(task, "copy_movie", lambda: self._copy(task, item), item_name=item["name"],
                           source_path=str(Path(task["source_path"]) / item["name"]), target_path=str(staging))
        self._checkpoint("after_copy", task, item)
        item["status"] = task["phase"] = "verifying"
        item["message"] = task["message"] = "正在读取目标并校验 SHA-256"
        self._save(task)
        def verify_movie():
            self._verify(task, item, staging)
            self._verify(task, item, Path(task["source_path"]) / item["name"], source=True)
        self._perform(task, "verify_movie", verify_movie, item_name=item["name"],
                           source_path=str(Path(task["source_path"]) / item["name"]), target_path=str(staging))
        workflow = task["_binding"].get("_workflow")
        if workflow:
            self._check_workflow_files(item, workflow, hashes=True)
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
                self._check_storage()
                self._perform(task, "commit_movie",
                    lambda: _rename_no_replace(staging, target / item["name"], item["staging_identity"]),
                    item_name=item["name"], source_path=str(staging), target_path=str(target / item["name"]))
        except FileExistsError:
            task["conflicts"] = [item["name"]]
            raise _Conflict("提交时出现同名目标，未覆盖，已停止队列", 409) from None
        # Keep the committing journal until cleaning has been durably written.
        # A process crash here is reconciled using the staged directory ID.
        item["committed"] = True
        self._checkpoint("after_commit", task, item)
        if workflow:
            item["status"] = "completed"
            item["message"] = "已归档；来源由 Taskpaw 精确清理"
            task["completed_count"] += 1
            task["receipt"] = self._workflow_receipt(task, item)
            self._save(task)
            return
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

    def storage_failed(self, message: str, task_id: str | None = None) -> None:
        """Freeze all operations; memory reports never pretend to be receipts."""
        with self._admission:
            self._storage_error = self._storage_error or ("任务状态或操作日志未能保存，传输已停止；请重启后核对结果：" + message)
            snapshots = dict(self._recovery_tasks)
            for identity, runtime in self._runtimes.items():
                runtime["stop"].set()
                snapshots[identity] = runtime["snapshot"]
                if runtime["worker"] is None:
                    runtime["finished"] = True
            for identity, snapshot in snapshots.items():
                task = copy.deepcopy(snapshot)
                task.update(status="needs_review", phase="finished", message=self._storage_error, stop_requested=True)
                for item in task["items"]:
                    if item["status"] in {"copying", "verifying", "committing", "cleaning"}:
                        item["status"] = "cleanup_pending" if item["status"] == "cleaning" else "needs_review"
                        item["message"] = self._storage_error
                self._fatal_tasks[identity] = task
            self._queue.clear()
            self._wake.set()

    def _run(self, task_id: str) -> None:
        with self._admission:
            if task_id in self._queue:
                self._queue.remove(task_id)
            runtime = self._runtimes.get(task_id)
            if runtime is None:
                return
            runtime["started"] = True
        try:
            self._execute(task_id)
        except Exception as exc:
            self.storage_failed(self._error_message(exc), task_id)
        finally:
            with self._admission:
                runtime["finished"] = True
                self._wake.set()

    def _execute(self, task_id: str) -> None:
        task = self._load(task_id)
        if task is None:
            return
        current = None
        try:
            self._check_stop(task)
            task["status"] = "running"
            task["phase"] = "preflight"
            task["message"] = "正在完整预检所选影片"
            self._save(task)
            self.audit.record(task, "transfer_started", "succeeded")
            self._preflight(task)
            for current in task["items"]:
                self._move_item(task, current)
            task["status"] = "completed"
            task["message"] = "影片包已归档到 NAS" if task["_binding"].get("_workflow") else "所选影片已全部移动完成"
        except _Stopped:
            task["status"] = "stopped"
            task["message"] = "已停止传输，未提交的源影片保留"
            if current and current["status"] != "completed":
                current["status"] = "stopped"
                current["message"] = task["message"]
        except AuditError:
            raise
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
        task["stop_requested"] = self._runtimes[task_id]["stop"].is_set() or task["stop_requested"]
        if task["conflicts"]:
            self.audit.record(task, "transfer_conflict", "failed", message=task["message"])
        outcome = {"completed": "succeeded", "failed": "failed", "stopped": "stopped", "needs_review": "needs_review"}[task["status"]]
        self.audit.record(task, "transfer_finished", outcome, message=task["message"])
        self._save(task)

    def recover(self) -> None:
        """Reconcile interrupted records, never resume or delete anything."""
        with self._admission:
            if self._runtimes or self._recovering or (self._recovery_worker and self._recovery_worker.is_alive()):
                return
            with self.db_lock:
                rows = self.conn.execute("SELECT payload FROM archive_tasks WHERE"
                    " json_extract(coalesce(public_payload,payload),'$.status') IN ('queued','running') ORDER BY rowid").fetchall()
            pending = [json.loads(row["payload"]) for row in rows]
            self._recovery_tasks = {task["id"]: task for task in pending}
            try:
                self.audit.recover_pending()
            except Exception as exc:
                self.storage_failed(str(exc))
                return
            if not pending:
                return
            self._recovering = True
            if self._auto_start:
                worker = threading.Thread(target=self._recover, args=(pending,), daemon=True, name="archive-recovery")
                try:
                    worker.start()
                    self._recovery_worker = worker
                except Exception as exc:
                    self._recovering = False
                    self.storage_failed(str(exc))
            else:
                self._recover(pending)

    def _recover(self, tasks: list[dict]) -> None:
        try:
            self._reconcile(tasks)
        except Exception as exc:
            self.storage_failed(self._error_message(exc))
        finally:
            with self._admission:
                self._recovering = False
                self._wake.set()

    def _reconcile(self, tasks: list[dict]) -> None:
        for task in tasks:
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
                    if task["_binding"].get("_workflow"):
                        item["status"] = "completed"
                        item["message"] = "已核对：影片包已完整归档"
                        task["receipt"] = self._workflow_receipt(task, item)
                        continue
                    original = Path(task["source_path"]) / item["name"]
                    try:
                        original.lstat()
                    except FileNotFoundError:
                        item["status"] = "completed"
                        item["message"] = "已核对：目标完整且源目录已清理"
                    else:
                        item["status"] = "cleanup_pending"
                        item["message"] = "目标已完整提交，本地源仍有残留，请手动核对"
                except AuditError:
                    raise
                except Exception as exc:
                    item["status"] = "needs_review"
                    item["message"] = f"上次传输中断，无法确认提交结果，请手动核对：{exc}"
            task["completed_count"] = sum(item["status"] == "completed" for item in task["items"])
            task["status"] = "completed" if task["completed_count"] == task["total_count"] else "needs_review"
            task["phase"] = "finished"
            task["message"] = "上次传输结果已核对" if task["status"] == "completed" else "上次传输中断，未自动继续或删除，请核对逐项结果"
            self.audit.record(task, "transfer_recovered", "succeeded" if task["status"] == "completed" else "needs_review",
                              message=task["message"])
            self._save(task)
