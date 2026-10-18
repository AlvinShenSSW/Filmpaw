"""Session-only cleanup of empty, first-level actors in an approved batch.

Windows deletion uses the same native handle whose identity, attributes, final
path and alternate streams were checked. There is no recursive delete, retry,
command journal or recovery that could act on newly-created directories later.
Operation receipts are persistent history only; they never authorize a delete.
"""

import copy
import ctypes
import os
import stat
import sys
import threading
import uuid
from contextlib import contextmanager
from pathlib import Path

from filmpaw_server.audit import AuditError
from filmpaw_server.directory_reads import directory_error_retryable
from filmpaw_server.windows_identity import NotPlainDirectory, directory_identity
from filmpaw_server.transfer import (
    NamedStreamsError,
    TransferError,
    TransferService,
    _check_named_streams,
    _directory_guard,
    _name,
)


CACHE_LIMIT = 128


class _SkipDirectory(TransferError):
    pass


def _kernel():
    if sys.platform != "win32":
        raise TransferError("当前平台不支持安全核验目录句柄，未清理任何目录")
    return ctypes.WinDLL("kernel32", use_last_error=True)


def _native_info(handle) -> dict:
    """Read identity and final path from this handle, never from path.stat()."""
    from ctypes import wintypes
    kernel = _kernel()
    try:
        identity = directory_identity(handle, kernel=kernel)
    except NotPlainDirectory as exc:
        raise _SkipDirectory(str(exc)) from None

    final_path = kernel.GetFinalPathNameByHandleW
    final_path.argtypes = [wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD]
    final_path.restype = wintypes.DWORD
    size = 512
    while size <= 32768:
        buffer = ctypes.create_unicode_buffer(size)
        length = final_path(handle, buffer, size, 0)
        if not length:
            raise ctypes.WinError(ctypes.get_last_error())
        if length < size:
            return {**identity, "path": buffer.value}
        size = length + 1
    raise TransferError("目录规范路径过长，无法安全核验，已保留")


@contextmanager
def _native_directory(path: Path, *, deleting: bool = False):
    from ctypes import wintypes
    kernel = _kernel()
    create = kernel.CreateFileW
    create.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                       wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    create.restype = wintypes.HANDLE
    close = kernel.CloseHandle
    close.argtypes = [wintypes.HANDLE]
    # LIST_DIRECTORY + READ_ATTRIBUTES (+ DELETE), READ sharing only;
    # BACKUP_SEMANTICS + OPEN_REPARSE_POINT. No reparse point is followed.
    handle = create(str(path), 0x81 | (0x10000 if deleting else 0), 1, None, 3, 0x02200000, None)
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        yield handle
    finally:
        close(handle)


def _same_path(first: str, second: str) -> bool:
    return os.path.normcase(os.path.normpath(first)) == os.path.normcase(os.path.normpath(second))


def _assert_binding(actual: dict, expected: dict) -> None:
    if actual["identity"] != expected["identity"] or not _same_path(actual["path"], expected["path"]):
        raise TransferError("目录句柄身份或位置已变化，已保留", 409)


def _assert_child(info: dict, root: dict) -> None:
    if not _same_path(str(Path(info["path"]).parent), root["path"]):
        raise TransferError("目录句柄不属于批准的 batch 一级目录，已保留", 409)


def _snapshot(root: dict) -> list[dict]:
    """Finish the entire root enumeration before deleting any candidate."""
    with os.scandir(root["path"]) as entries:
        snapshot = sorted(list(entries), key=lambda entry: entry.name.casefold())
    result = []
    for entry in snapshot:
        item = {"name": entry.name}
        try:
            info = entry.stat(follow_symlinks=False)
            attrs = getattr(info, "st_file_attributes", 0)
            if not (stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode) or attrs & 0x400):
                continue
            if stat.S_ISLNK(info.st_mode) or attrs & 0x400:
                raise _SkipDirectory("链接或重解析点，已跳过")
            if entry.name.startswith(".") or attrs & 6:
                raise _SkipDirectory("隐藏或系统目录，已跳过")
            _name(entry.name)
            with _native_directory(Path(root["path"]) / entry.name) as handle:
                item["binding"] = _native_info(handle)
                _assert_child(item["binding"], root)
        except _SkipDirectory as exc:
            item.update(status="skipped", message=str(exc))
        except FileNotFoundError:
            item.update(status="skipped", message="目录已不存在，未计为本次删除")
        except (OSError, TransferError) as exc:
            item.update(status="failed", message=str(exc))
        result.append(item)
    return result


def _remove_empty(root: dict, candidate: dict, checkpoint) -> tuple[str, str]:
    """Check and remove through the exact DELETE handle; root guard is held."""
    from ctypes import wintypes
    path = Path(root["path"]) / candidate["name"]
    with _native_directory(path, deleting=True) as handle:
        actual = _native_info(handle)
        _assert_binding(actual, candidate["binding"])
        _assert_child(actual, root)
        if actual["attributes"] & 6:
            return "skipped", "目录变为隐藏或系统目录，已保留"
        with os.scandir(path) as entries:
            if next(entries, None) is not None:
                return "not_empty", "目录含文件或子目录，已保留"
        checkpoint()
        try:
            _check_named_streams(handle, path)
        except NamedStreamsError:
            return "not_empty", "目录带有附加数据流，已保留"
        kernel = _kernel()
        set_info = kernel.SetFileInformationByHandle
        set_info.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        set_info.restype = wintypes.BOOL
        delete = ctypes.c_ubyte(1)
        if not set_info(handle, 4, ctypes.byref(delete), ctypes.sizeof(delete)):
            error = ctypes.get_last_error()
            if error == 145:  # ERROR_DIR_NOT_EMPTY: another writer added content
                return "not_empty", "检查后目录新增内容，已保留"
            raise ctypes.WinError(error)
    return "deleted", "已删除空目录"


class CleanupService:
    def __init__(self, transfers: TransferService, *, auto_start: bool = True):
        self.transfers = transfers
        self._auto_start = auto_start
        self._lock = threading.RLock()
        self._read_lock = threading.Lock()
        self._contexts = {}
        self._tasks = {}
        self._worker = None
        self._closed = False

    def _settings(self) -> tuple[str | None, str]:
        with self.transfers.db_lock:
            settings = dict(self.transfers.conn.execute(
                "SELECT key,value FROM settings WHERE key IN ('last_local_dir','last_local_dir_revision')"
            ).fetchall())
        return settings.get("last_local_dir"), settings.get("last_local_dir_revision") or "legacy"

    def _ensure_open(self) -> None:
        if self._closed:
            raise TransferError("应用正在退出，无法清理目录", 409)
        self.transfers.ensure_open()

    def create_context(self, batch_path: str) -> dict:
        with self._lock:
            self._ensure_open()
            approved, revision = self._settings()
            path = Path(batch_path)
            if not approved or approved != batch_path:
                raise TransferError("batch 目录已变化，请重新选择", 409)
            if not path.is_absolute() or path.parent == path:
                raise TransferError("必须选择非磁盘／共享根的绝对 batch 目录")
        if not self._read_lock.acquire(blocking=False):
            raise TransferError("batch 目录仍在读取，请稍候重试", 503)
        try:
            try:
                # Validate original ancestors before obtaining the handle's
                # normalized path; resolving first could hide a junction.
                with _directory_guard(path) as handle:
                    binding = _native_info(handle)
                    native_path = Path(binding["path"])
                    if native_path.parent == native_path:
                        raise TransferError("不能清理磁盘或共享根目录")
            except OSError as exc:
                raise TransferError(str(exc), 503 if directory_error_retryable(exc) else 422) from None
            with self._lock:
                self._ensure_open()
                if self._settings() != (approved, revision):
                    raise TransferError("读取期间 batch 目录已变化，请重新选择", 409)
                context = {"id": str(uuid.uuid4()), "batch_path": approved, "batch_revision": revision}
                if len(self._contexts) >= CACHE_LIMIT:
                    self._contexts.pop(next(iter(self._contexts)))
                self._contexts[context["id"]] = {**context, "_binding": binding}
                return context
        finally:
            self._read_lock.release()

    @staticmethod
    def _public(task: dict) -> dict:
        return copy.deepcopy({key: value for key, value in task.items() if not key.startswith("_")})

    def submit(self, context_id: str, request_id: str) -> dict:
        if not context_id or not request_id or len(context_id) > 128 or len(request_id) > 128:
            raise TransferError("无效的清理上下文或幂等标识")
        with self._lock:
            previous = next((task for task in self._tasks.values() if task["request_id"] == request_id), None)
            if previous:
                if previous["context_id"] != context_id:
                    raise TransferError("幂等标识已用于另一清理上下文", 409)
                return self._public(previous)
            self._ensure_open()
            context = self._contexts.get(context_id)
            if context is None:
                raise TransferError("清理上下文已失效或已使用，请重新读取目录", 409)
            task = {"id": str(uuid.uuid4()), "context_id": context_id, "request_id": request_id,
                    "batch_path": context["batch_path"], "batch_revision": context["batch_revision"],
                    "status": "queued", "current_name": None, "checked_count": 0, "total_count": 0,
                    "deleted_count": 0, "kept_count": 0, "skipped_count": 0, "failed_count": 0,
                    "message": "等待检查当前 batch 的空演员目录", "items": [],
                    "_binding": copy.deepcopy(context["_binding"])}
            self.transfers.reserve_cleanup(task["id"], task["batch_path"], task["batch_revision"])
            self._contexts.pop(context_id)
            if len(self._tasks) >= CACHE_LIMIT:
                self._tasks.pop(next(iter(self._tasks)))
            self._tasks[task["id"]] = task
            try:
                self._record(task, "cleanup_accepted", "succeeded", message="已接受空演员目录清理任务")
            except AuditError as exc:
                task.update(status="failed", message=str(exc))
                self.transfers.release_cleanup(task["id"])
                return self._public(task)
            if self._auto_start:
                self._worker = threading.Thread(target=self._run, args=(task["id"],),
                                                daemon=True, name="empty-actor-cleanup")
                try:
                    self._worker.start()
                except Exception as exc:
                    # A Thread whose start failed cannot be joined at shutdown.
                    self._worker = None
                    task.update(status="failed", message=f"清理工作器未能启动，未删除目录：{exc}")
                    try:
                        self._record(task, "cleanup_finished", "failed", message=task["message"])
                    except AuditError as log_error:
                        task["message"] += f"；{log_error}"
                    self.transfers.release_cleanup(task["id"])
            return self._public(task)

    def get_task(self, id: str | None = None) -> dict | None:
        with self._lock:
            task = self._tasks.get(id) if id is not None else next(reversed(self._tasks.values()), None)
            if id is not None and task is None:
                raise TransferError("本次会话找不到该清理任务，请重新读取目录核对", 404)
            return self._public(task) if task else None

    def _publish(self, task: dict) -> None:
        with self._lock:
            self._tasks[task["id"]] = copy.deepcopy(task)

    def _checkpoint(self, stage: str, task: dict, item: dict) -> None:
        """Failure injection at mutation boundaries, not an extension API."""

    def _record(self, task: dict, action: str, outcome: str, **fields) -> None:
        try:
            self.transfers.audit.record(task, action, outcome, kind="cleanup", **fields)
        except AuditError as exc:
            self.transfers.storage_failed(str(exc))
            raise

    def _run(self, task_id: str) -> None:
        with self._lock:
            stored = self._tasks.get(task_id)
            if stored is None or stored["status"] != "queued":
                return
            stored["status"] = "running"
            task = copy.deepcopy(stored)
        try:
            self._record(task, "cleanup_started", "succeeded", message="开始检查当前 batch")
            root = task["_binding"]
            # The native final path freezes mapped-drive resolution. Keep all
            # ancestors and this root guarded throughout snapshot and deletes.
            with _directory_guard(Path(root["path"])) as handle:
                _assert_binding(_native_info(handle), root)
                candidates = _snapshot(root)
                task["total_count"] = len(candidates)
                task["message"] = "正在检查并清理空演员目录"
                self._publish(task)
                for candidate in candidates:
                    task["current_name"] = candidate["name"]
                    self._publish(task)
                    status, message = candidate.get("status"), candidate.get("message")
                    event_task = {**task, "performer_name": candidate["name"]}
                    event_fields = {"item_name": candidate["name"],
                                    "source_path": str(Path(root["path"]) / candidate["name"])}
                    def before_delete():
                        self._checkpoint("before_delete", task, candidate)
                        self._record(event_task, "delete_empty_actor", "started", **event_fields,
                                     message="准备删除空演员目录，结果尚未确认")
                    if not status:
                        try:
                            status, message = _remove_empty(root, candidate, before_delete)
                        except _SkipDirectory as exc:
                            status, message = "skipped", str(exc)
                        except FileNotFoundError:
                            status, message = "skipped", "目录已不存在，未计为本次删除"
                        except (OSError, TransferError) as exc:
                            status, message = "failed", str(exc)
                    outcome = "succeeded" if status == "deleted" else "failed" if status == "failed" else "skipped"
                    self._record(event_task, "delete_empty_actor", outcome, **event_fields, message=message)
                    task["items"].append({"name": candidate["name"], "status": status, "message": message})
                    count = {"deleted": "deleted_count", "not_empty": "kept_count",
                             "skipped": "skipped_count", "failed": "failed_count"}[status]
                    task[count] += 1
                    task["checked_count"] += 1
                    self._publish(task)
            task["status"] = "completed"
            task["message"] = (f"清理完成，已删除 {task['deleted_count']} 个空目录"
                               if task["deleted_count"] else "没有可清理的空目录，请查看保留与失败明细")
        except AuditError as exc:
            task["status"] = "failed"
            task["message"] = f"清理已停止，最后操作结果待核对：{exc}"
            if task["current_name"]:
                task["items"].append({"name": task["current_name"], "status": "failed",
                                      "message": "操作日志未能保存，删除结果待核对；后续目录未处理"})
                task["failed_count"] += 1
                task["checked_count"] += 1
        except Exception as exc:
            task["status"] = "failed"
            task["message"] = f"清理已停止，未确认的目录保留：{exc}"
        finally:
            if not self.transfers.audit.failed:
                try:
                    self._record(task, "cleanup_finished", "succeeded" if task["status"] == "completed" else "failed",
                                 message=task["message"])
                except AuditError as exc:
                    task.update(status="failed", message=f"清理结束状态未能保存，请核对已记录结果：{exc}")
            task["current_name"] = None
            self._publish(task)
            self.transfers.release_cleanup(task_id)

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self.transfers.begin_close()
            worker = self._worker
            if worker is None:
                # Deterministic tests can disable worker startup. Closing a
                # queued test service cancels admission without filesystem I/O.
                for task in self._tasks.values():
                    if task["status"] == "queued":
                        task.update(status="failed", message="服务关闭，清理尚未开始")
                        self.transfers.release_cleanup(task["id"])
        if worker and worker is not threading.current_thread():
            worker.join()
