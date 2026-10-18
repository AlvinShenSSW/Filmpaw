"""Archive moves use disposable folders and explicit failure injection only."""

import shutil
import subprocess
import sys
import threading
from contextlib import contextmanager
from pathlib import Path

import pytest

from filmpaw_server.transfer import TransferError, TransferService


@pytest.fixture()
def archive(db, tmp_path):
    batch = tmp_path / "batch"
    source = batch / "Actor"
    target = tmp_path / "nas" / "Actor"
    source.mkdir(parents=True)
    target.mkdir(parents=True)
    (source / "folder.jpg").write_bytes(b"actor portrait")
    db.execute("INSERT INTO settings(key,value) VALUES ('last_local_dir',?)", (str(batch),))
    db.execute("INSERT INTO sources(id,unc_path,label) VALUES (1,?,'NAS')", (str(target.parent),))
    db.execute(
        "INSERT INTO performers(id,name,name_norm,source_id,folder_name,unc_path,first_seen_at,last_seen_at)"
        " VALUES ('actor','Actor','actor',1,'Actor',?,'now','now')", (str(target),)
    )
    db.commit()
    service = TransferService(db, threading.RLock(), auto_start=False)
    yield service, batch, source, target, db
    service.close()


def movie(root, name="MOV-001"):
    path = root / name
    (path / "subs").mkdir(parents=True)
    (path / "empty").mkdir()
    (path / "video.mp4").write_bytes(bytes(range(256)) * 50)
    (path / "movie.nfo").write_text("<movie>metadata</movie>", encoding="utf-8")
    (path / "poster.jpg").write_bytes(b"poster")
    (path / "subs" / "zh.srt").write_text("subtitle", encoding="utf-8")
    return path


def context(archive):
    service, batch, *_ = archive
    return service.create_context(str(batch), "Actor", "actor")


def submit(archive, names=None):
    service = archive[0]
    return service.submit(context(archive)["id"], names or ["MOV-001"], "request-1")


def run(archive, task):
    archive[0]._run(task["id"])
    return archive[0].get_task(task["id"])


def test_complete_movie_tree_moves_and_actor_files_remain(archive):
    service, _, source, target, _ = archive
    original = movie(source)
    expected = {str(p.relative_to(original)): p.read_bytes() for p in original.rglob("*") if p.is_file()}
    task = run(archive, submit(archive))
    assert task["status"] == "completed", task["message"]
    assert not original.exists()
    assert (source / "folder.jpg").read_bytes() == b"actor portrait"
    assert (target / "MOV-001" / "empty").is_dir()
    copied = target / "MOV-001"
    assert {str(p.relative_to(copied)): p.read_bytes() for p in copied.rglob("*") if p.is_file()} == expected
    assert task["completed_count"] == 1
    assert task["progress_bytes"] == task["total_bytes"] > 0
    assert "manifest" not in task["items"][0]


def test_preflight_conflict_stops_entire_batch_case_insensitively(archive):
    _, _, source, target, _ = archive
    movie(source, "MOV-001")
    movie(source, "MOV-002")
    (target / "mov-002").write_bytes(b"existing file")
    task = run(archive, submit(archive, ["MOV-001", "MOV-002"]))
    assert task["status"] == "failed"
    assert task["conflicts"] == ["MOV-002"]
    assert not (target / "MOV-001").exists()
    assert (source / "MOV-001").is_dir() and (source / "MOV-002").is_dir()


def test_runtime_conflict_preserves_source_and_existing_destination(archive, monkeypatch):
    service, _, source, target, _ = archive
    movie(source)
    movie(source, "MOV-002")
    def conflict(stage, task, item):
        if stage == "before_commit":
            (target / item["name"]).mkdir()
            (target / item["name"] / "existing").write_bytes(b"keep")
    monkeypatch.setattr(service, "_checkpoint", conflict)
    task = run(archive, submit(archive, ["MOV-001", "MOV-002"]))
    assert task["status"] == "failed"
    assert task["conflicts"] == ["MOV-001"]
    assert (target / "MOV-001" / "existing").read_bytes() == b"keep"
    assert (source / "MOV-001" / "video.mp4").exists()
    assert not (target / "MOV-002").exists()


@pytest.mark.parametrize("name", ["..", "../other", "a/b", "a\\b", "C:movie", "", "."])
def test_invalid_movie_names_rejected(archive, name):
    movie(archive[2])
    with pytest.raises(TransferError):
        archive[0].submit(context(archive)["id"], [name], "bad-request")


def test_stale_settings_and_target_binding_rejected(archive):
    service, _, source, _, db = archive
    movie(source)
    ctx = context(archive)
    db.execute("UPDATE settings SET value='different' WHERE key='last_local_dir'")
    db.commit()
    with pytest.raises(TransferError, match="目录|失效|context"):
        service.submit(ctx["id"], ["MOV-001"], "request-1")


def test_duplicate_request_returns_same_task_and_second_queue_is_blocked(archive):
    service, _, source, _, _ = archive
    movie(source)
    ctx = context(archive)
    first = service.submit(ctx["id"], ["MOV-001"], "request-1")
    assert service.submit(ctx["id"], ["MOV-001"], "request-1")["id"] == first["id"]
    with pytest.raises(TransferError):
        service.submit(ctx["id"], ["MOV-001"], "request-2")
    with pytest.raises(TransferError):
        service.submit(ctx["id"], ["other"], "request-1")


@pytest.mark.parametrize("stage", ["before_copy", "after_copy", "after_verify"])
def test_errors_before_commit_never_remove_source(archive, monkeypatch, stage):
    service, _, source, target, _ = archive
    movie(source)
    def fail(actual, task, item):
        if actual == stage:
            raise OSError("injected failure")
    monkeypatch.setattr(service, "_checkpoint", fail)
    task = run(archive, submit(archive))
    assert task["status"] == "failed"
    assert (source / "MOV-001" / "video.mp4").exists()
    assert not (target / "MOV-001").exists()


def test_changed_source_during_copy_is_preserved(archive, monkeypatch):
    service, _, source, target, _ = archive
    movie(source)
    def alter(stage, task, item):
        if stage == "after_copy":
            (source / "MOV-001" / "video.mp4").write_bytes(b"download changed")
    monkeypatch.setattr(service, "_checkpoint", alter)
    task = run(archive, submit(archive))
    assert task["status"] == "failed"
    assert (source / "MOV-001" / "video.mp4").read_bytes() == b"download changed"
    assert not (target / "MOV-001").exists()


def test_corrupted_target_fails_verification_without_deleting_source(archive, monkeypatch):
    service, _, source, target, _ = archive
    movie(source)
    def corrupt(stage, task, item):
        if stage == "after_copy":
            (Path(item["staging_path"]) / "video.mp4").write_bytes(b"bad copy")
    monkeypatch.setattr(service, "_checkpoint", corrupt)
    task = run(archive, submit(archive))
    assert task["status"] == "failed"
    assert (source / "MOV-001" / "video.mp4").stat().st_size > 10000
    assert not (target / "MOV-001").exists()


def test_added_source_file_after_commit_reports_cleanup_pending(archive, monkeypatch):
    service, _, source, target, _ = archive
    movie(source)
    def add(stage, task, item):
        if stage == "after_commit":
            (source / "MOV-001" / "new.nfo").write_bytes(b"new data")
    monkeypatch.setattr(service, "_checkpoint", add)
    task = run(archive, submit(archive))
    assert task["status"] == "needs_review"
    assert task["items"][0]["status"] == "cleanup_pending"
    assert (target / "MOV-001" / "video.mp4").exists()
    assert (source / "MOV-001" / "new.nfo").read_bytes() == b"new data"


def test_stop_before_copy_preserves_entire_queue(archive):
    service, _, source, target, _ = archive
    movie(source)
    task = submit(archive)
    service.stop(task["id"])
    result = run(archive, task)
    assert result["status"] == "stopped"
    assert (source / "MOV-001").exists() and not (target / "MOV-001").exists()


class PowerLoss(BaseException):
    pass


@pytest.mark.parametrize("stage,expected", [("after_commit", "cleanup_pending"), ("after_cleanup", "completed")])
def test_restart_reconciles_commit_and_cleanup_windows_without_deleting(archive, monkeypatch, stage, expected):
    service, _, source, target, db = archive
    movie(source)
    def crash(actual, task, item):
        if actual == stage:
            raise PowerLoss()
    monkeypatch.setattr(service, "_checkpoint", crash)
    task = submit(archive)
    with pytest.raises(PowerLoss):
        service._run(task["id"])
    restored = TransferService(db, threading.RLock(), auto_start=False)
    restored.recover()
    result = restored.get_task(task["id"])
    assert result["items"][0]["status"] == expected
    assert (target / "MOV-001" / "video.mp4").exists()
    assert (source / "MOV-001").exists() == (expected == "cleanup_pending")
    restored.close()


def test_recovery_never_trusts_unrelated_same_name_directory(archive, monkeypatch):
    service, _, source, target, db = archive
    movie(source)
    def crash(stage, task, item):
        if stage == "before_commit":
            movie(target)
            raise PowerLoss()
    monkeypatch.setattr(service, "_checkpoint", crash)
    task = submit(archive)
    with pytest.raises(PowerLoss):
        service._run(task["id"])
    restored = TransferService(db, threading.RLock(), auto_start=False)
    restored.recover()
    assert restored.get_task(task["id"])["items"][0]["status"] == "needs_review"
    assert (source / "MOV-001").exists()
    restored.close()


def test_preflight_unreadable_later_movie_prevents_all_moves(archive, monkeypatch):
    import filmpaw_server.transfer as transfer
    _, _, source, target, _ = archive
    movie(source)
    movie(source, "MOV-002")
    original = transfer._open_read
    @contextmanager
    def denied(path, **kwargs):
        if "MOV-002" in path.parts:
            raise PermissionError("source is locked")
        with original(path, **kwargs) as stream:
            yield stream
    monkeypatch.setattr(transfer, "_open_read", denied)
    task = run(archive, submit(archive, ["MOV-001", "MOV-002"]))
    assert task["status"] == "failed"
    assert (source / "MOV-001" / "video.mp4").exists()
    assert not (target / "MOV-001").exists()


def test_preflight_disk_full_prevents_any_movie_move(archive, monkeypatch):
    import filmpaw_server.transfer as transfer
    _, _, source, target, _ = archive
    movie(source)
    monkeypatch.setattr(transfer.shutil, "disk_usage", lambda _: shutil._ntuple_diskusage(100, 100, 0))
    task = run(archive, submit(archive))
    assert task["status"] == "failed"
    assert (source / "MOV-001").exists() and not (target / "MOV-001").exists()


def test_source_replaced_after_commit_is_not_deleted(archive, monkeypatch):
    service, _, source, target, _ = archive
    movie(source)
    def replace(stage, task, item):
        if stage == "after_commit":
            (source / "MOV-001").rename(source / "original")
            movie(source)
            (source / "MOV-001" / "keep.txt").write_bytes(b"not this job")
    monkeypatch.setattr(service, "_checkpoint", replace)
    task = run(archive, submit(archive))
    assert task["items"][0]["status"] == "cleanup_pending"
    assert (source / "original" / "video.mp4").exists()
    assert (source / "MOV-001" / "keep.txt").read_bytes() == b"not this job"
    assert (target / "MOV-001" / "video.mp4").exists()


def test_source_file_replaced_after_commit_is_not_deleted(archive, monkeypatch):
    service, _, source, target, _ = archive
    movie(source)
    def replace(stage, task, item):
        if stage == "after_commit":
            original = source / "MOV-001" / "movie.nfo"
            original.rename(original.with_name("old.nfo"))
            original.write_bytes(b"a new file")
    monkeypatch.setattr(service, "_checkpoint", replace)
    task = run(archive, submit(archive))
    assert task["items"][0]["status"] == "cleanup_pending"
    assert (source / "MOV-001" / "movie.nfo").read_bytes() == b"a new file"


def test_symlink_tree_rejected_before_any_move(archive):
    _, _, source, target, _ = archive
    first = movie(source)
    movie(source, "MOV-002")
    link = source / "MOV-002" / "link"
    try:
        link.symlink_to(first, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks unavailable; Windows junction coverage runs separately")
    task = run(archive, submit(archive, ["MOV-001", "MOV-002"]))
    assert task["status"] == "failed"
    assert first.exists() and not (target / "MOV-001").exists()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows junction safety")
def test_windows_junction_is_rejected_without_following_it(archive, tmp_path):
    _, _, source, target, _ = archive
    movie(source)
    link = source / "MOV-001" / "junction"
    external = tmp_path / "unrelated"
    external.mkdir()
    (external / "keep.txt").write_bytes(b"external data")
    # This creates a disposable junction; there is no recursive deletion or
    # shell-built file operation. pytest's cleanup handles its own temp tree.
    result = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(external)],
                            stdin=subprocess.DEVNULL, capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr
    try:
        task = run(archive, submit(archive))
        assert task["status"] == "failed"
        assert (external / "keep.txt").read_bytes() == b"external data"
        assert not (target / "MOV-001").exists()
    finally:
        link.rmdir()  # remove only the junction itself, not its target


@pytest.mark.skipif(sys.platform != "win32", reason="Windows sharing rules")
def test_directory_guard_blocks_parent_rename(archive):
    from filmpaw_server.transfer import _directory_guard
    source = archive[2]
    original = movie(source)
    with _directory_guard(original):
        with pytest.raises(PermissionError):
            original.rename(source / "other")
    assert original.is_dir()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows sharing rules")
def test_read_handle_blocks_concurrent_writes_and_replacement(archive):
    from filmpaw_server.transfer import _open_read
    original = movie(archive[2]) / "video.mp4"
    with _open_read(original):
        with pytest.raises(PermissionError):
            original.write_bytes(b"changed")
        with pytest.raises(PermissionError):
            original.rename(original.with_name("other.mp4"))


def test_stop_request_does_not_overwrite_newer_terminal_state(archive, monkeypatch):
    service, _, source, _, _ = archive
    movie(source)
    task = submit(archive)
    original_load = service._load
    advanced = False
    def finish_after_read(*args, **kwargs):
        nonlocal advanced
        result = original_load(*args, **kwargs)
        if not advanced:
            advanced = True
            latest = dict(result)
            latest["status"] = "completed"
            latest["phase"] = "finished"
            service._save(latest)
        return result
    monkeypatch.setattr(service, "_load", finish_after_read)
    service.stop(task["id"])
    assert original_load(task["id"])["status"] == "completed"


def test_exit_admission_blocks_new_jobs_only_when_idle(archive):
    service, _, source, _, _ = archive
    movie(source)
    ctx = context(archive)
    assert service.prepare_exit() == {"safe": True, "task_id": None}
    with pytest.raises(TransferError):
        service.submit(ctx["id"], ["MOV-001"], "new")


def test_exit_waits_for_active_task(archive):
    service, _, source, _, _ = archive
    movie(source)
    task = submit(archive)
    assert service.prepare_exit() == {"safe": False, "task_id": task["id"]}
    service.stop(task["id"])
    run(archive, task)
    assert service.prepare_exit()["safe"]


def test_background_io_does_not_hold_database_lock(archive, monkeypatch):
    service, _, source, _, _ = archive
    movie(source)
    service._auto_start = True
    reached = threading.Event()
    release = threading.Event()
    def pause(stage, task, item):
        if stage == "before_copy":
            reached.set()
            assert release.wait(5), "test worker release timed out"
    monkeypatch.setattr(service, "_checkpoint", pause)
    task = submit(archive)
    try:
        assert reached.wait(5)
        assert service.db_lock.acquire(timeout=0.1)
        try:
            assert service.get_task(task["id"])["status"] == "running"
        finally:
            service.db_lock.release()
        service.stop(task["id"])
    finally:
        release.set()
        service._worker.join(5)
    assert service.get_task(task["id"])["status"] == "stopped"


def test_revision_rejects_old_context_even_if_batch_path_returns_to_same_value(archive):
    service, _, source, _, db = archive
    movie(source)
    ctx = context(archive)
    db.execute("INSERT INTO settings(key,value) VALUES ('last_local_dir_revision','changed-twice')")
    db.commit()
    with pytest.raises(TransferError):
        service.submit(ctx["id"], ["MOV-001"], "request-1")
    assert "_batch_revision" not in ctx


def test_exit_identifies_task_in_worker_finalization_window(archive):
    service, _, source, _, _ = archive
    movie(source)
    task = run(archive, submit(archive))
    class FinalizingWorker:
        def is_alive(self):
            return True
        def join(self):
            pass
    service._worker = FinalizingWorker()
    assert service.prepare_exit() == {"safe": False, "task_id": task["id"]}


def test_native_commit_never_replaces_destination_created_at_last_instant(archive, monkeypatch):
    import filmpaw_server.transfer as transfer
    _, _, source, target, _ = archive
    movie(source)
    actual = transfer._rename_no_replace
    def concurrent_create(staging, destination, identity):
        destination.mkdir()
        (destination / "keep").write_bytes(b"existing content")
        actual(staging, destination, identity)
    monkeypatch.setattr(transfer, "_rename_no_replace", concurrent_create)
    task = run(archive, submit(archive))
    assert task["status"] == "failed", task["message"]
    assert task["conflicts"] == ["MOV-001"]
    assert (target / "MOV-001" / "keep").read_bytes() == b"existing content"
    assert (source / "MOV-001" / "video.mp4").exists()


def test_committed_journal_failure_preserves_source_and_reports_cleanup_pending(archive, monkeypatch):
    service, _, source, target, _ = archive
    movie(source)
    original = service._save
    failed = False
    def fail_cleaning_journal(task):
        nonlocal failed
        if task["phase"] == "cleaning" and not failed:
            failed = True
            raise OSError("journal unavailable")
        original(task)
    monkeypatch.setattr(service, "_save", fail_cleaning_journal)
    task = run(archive, submit(archive))
    assert task["status"] == "needs_review"
    assert task["items"][0]["status"] == "cleanup_pending"
    assert (source / "MOV-001" / "video.mp4").exists()
    assert (target / "MOV-001" / "video.mp4").exists()


def test_partial_cleanup_error_keeps_remaining_source_and_stops_later_movies(archive, monkeypatch):
    import filmpaw_server.transfer as transfer
    _, _, source, target, _ = archive
    movie(source)
    movie(source, "MOV-002")
    original = transfer._delete_exact
    calls = 0
    def fail_second(original_path, destination, entry):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise PermissionError("file is occupied")
        original(original_path, destination, entry)
    monkeypatch.setattr(transfer, "_delete_exact", fail_second)
    task = run(archive, submit(archive, ["MOV-001", "MOV-002"]))
    assert task["items"][0]["status"] == "cleanup_pending"
    assert task["items"][1]["status"] == "pending"
    assert (target / "MOV-001" / "video.mp4").exists()
    assert (source / "MOV-001").exists() and (source / "MOV-002").exists()
    assert not (target / "MOV-002").exists()


def test_stop_at_movie_boundary_retains_completed_movies(archive, monkeypatch):
    service, _, source, target, _ = archive
    movie(source)
    movie(source, "MOV-002")
    def stop_after_cleanup(stage, task, item):
        if stage == "after_cleanup":
            service.stop(task["id"])
    monkeypatch.setattr(service, "_checkpoint", stop_after_cleanup)
    task = run(archive, submit(archive, ["MOV-001", "MOV-002"]))
    assert task["status"] == "stopped"
    assert task["completed_count"] == 1
    assert task["items"][0]["status"] == "completed"
    assert not (source / "MOV-001").exists()
    assert (target / "MOV-001").exists() and (source / "MOV-002").exists()
    assert not (target / "MOV-002").exists()


def test_frozen_task_binding_survives_settings_change_after_submission(archive):
    _, _, source, target, db = archive
    movie(source)
    task = submit(archive)
    db.execute("UPDATE settings SET value='different batch' WHERE key='last_local_dir'")
    db.commit()
    task = run(archive, task)
    assert task["status"] == "completed", task["message"]
    assert (target / "MOV-001").exists()


def test_offline_target_is_separate_from_empty_or_readable_source(archive):
    service, _, source, target, _ = archive
    movie(source)
    target.rmdir()
    ctx = context(archive)
    assert ctx["source_error"] is None
    assert [item["name"] for item in ctx["source_items"]] == ["MOV-001"]
    assert ctx["target_error"] and ctx["target_items"] == []
    with pytest.raises(TransferError):
        service.submit(ctx["id"], ["MOV-001"], "offline")


@pytest.mark.parametrize("name", ["作品 001, 特别版", "Movie-02-😀"])
def test_unicode_spaces_commas_and_surrogate_pairs_keep_exact_names(archive, name):
    _, _, source, target, _ = archive
    movie(source, name)
    task = run(archive, submit(archive, [name]))
    assert task["status"] == "completed", task["message"]
    assert (target / name / "video.mp4").exists()
    assert not (source / name).exists()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows in-place reparse protection")
def test_directory_guard_denies_write_handle_needed_for_in_place_reparse(archive):
    import ctypes
    from ctypes import wintypes
    from filmpaw_server.transfer import _directory_guard
    root = movie(archive[2]) / "empty"
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    create = kernel.CreateFileW
    create.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                       wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    create.restype = wintypes.HANDLE
    with _directory_guard(root):
        handle = create(str(root), 0x40000000, 7, None, 3, 0x02200000, None)
        if handle != ctypes.c_void_p(-1).value:
            close = kernel.CloseHandle
            close.argtypes = [wintypes.HANDLE]
            close(handle)
            pytest.fail("an in-place junction writer was allowed through the directory guard")
        assert ctypes.get_last_error() == 32


@pytest.mark.parametrize("failure_phase", ["committing", "finished"])
def test_unwritable_journal_is_visible_and_cannot_strand_exit(archive, monkeypatch, failure_phase):
    service, _, source, target, db = archive
    movie(source)
    task = submit(archive)
    original = service._save
    broken = False
    def disk_failed(payload):
        nonlocal broken
        if payload["phase"] == failure_phase:
            broken = True
        if broken:
            raise OSError("persistent journal write failure")
        original(payload)
    monkeypatch.setattr(service, "_save", disk_failed)
    service._run(task["id"])
    report = service.get_task()
    assert report["status"] == "needs_review"
    assert "未能保存" in report["message"]
    assert service._load(task["id"])["status"] == "running", "old durable journal must remain unchanged"
    with pytest.raises(TransferError):
        service.submit(task["context_id"], ["MOV-001"], "new-request")
    assert service.prepare_exit() == {"safe": True, "task_id": None}
    assert (source / "MOV-001").exists() == (failure_phase == "committing")
    assert (target / "MOV-001").exists() == (failure_phase == "finished")
    restored = TransferService(db, threading.RLock(), auto_start=False)
    restored.recover()
    result = restored.get_task(task["id"])
    assert result["status"] == ("completed" if failure_phase == "finished" else "needs_review")
    restored.close()
