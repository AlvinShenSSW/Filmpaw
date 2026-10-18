"""Empty-actor cleanup only touches disposable, explicitly approved batches."""

import json
import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from filmpaw_server.transfer import TransferError, TransferService


pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows desktop native cleanup")


@pytest.fixture()
def cleanup_env(db, tmp_path):
    from filmpaw_server.cleanup import CleanupService
    batch = tmp_path / "batch"
    batch.mkdir()
    db.execute("INSERT INTO settings VALUES ('last_local_dir',?)", (str(batch),))
    db.execute("INSERT INTO settings VALUES ('last_local_dir_revision','revision-one')")
    db.commit()
    transfers = TransferService(db, threading.RLock(), auto_start=False)
    service = CleanupService(transfers, auto_start=False)
    yield service, transfers, batch, db
    service.close()
    transfers.close()


def start(env, *, request_id="cleanup-request"):
    service, _, batch, _ = env
    context = service.create_context(str(batch))
    return service.submit(context["id"], request_id)


def finish(env, task=None):
    service = env[0]
    task = task or start(env)
    service._run(task["id"])
    return service.get_task(task["id"])


def test_cleanup_log_records_deleted_and_kept_names_durably(cleanup_env):
    _, transfers, batch, db = cleanup_env
    from filmpaw_server.audit import OperationLog
    (batch / "Empty").mkdir()
    (batch / "Keep").mkdir()
    (batch / "Keep" / "folder.jpg").write_bytes(b"keep")
    result = finish(cleanup_env)
    log = OperationLog(db, transfers.db_lock)
    rows = log.list(task_id=result["id"], action="delete_empty_actor")["items"]
    assert [(row["item_name"], row["outcome"]) for row in rows] == [
        ("Keep", "skipped"), ("Empty", "succeeded"), ("Empty", "started"),
    ]
    assert all(row["kind"] == "cleanup" for row in rows)
    assert all(row["source_path"].endswith(row["item_name"]) for row in rows)


@pytest.mark.parametrize("fail_outcome,first_deleted", [("started", False), ("succeeded", True)])
def test_cleanup_log_failure_halts_next_delete_and_never_claims_success(cleanup_env, fail_outcome, first_deleted):
    _, transfers, batch, db = cleanup_env
    for name in ["A", "B"]:
        (batch / name).mkdir()
    db.execute("CREATE TRIGGER break_cleanup_log BEFORE INSERT ON operation_events "
               f"WHEN NEW.action='delete_empty_actor' AND NEW.outcome='{fail_outcome}' "
               "BEGIN SELECT RAISE(FAIL,'disk full'); END")
    db.commit()
    result = finish(cleanup_env)
    assert result["status"] == "failed"
    assert result["deleted_count"] == 0
    assert (batch / "A").exists() != first_deleted
    assert (batch / "B").is_dir()
    assert all(item["status"] != "deleted" for item in result["items"])
    assert transfers.audit.failed
    assert not transfers.audit.list(task_id=result["id"], outcome="succeeded")["items"] or all(
        row["action"] != "delete_empty_actor"
        for row in transfers.audit.list(task_id=result["id"], outcome="succeeded")["items"]
    )
    with pytest.raises(TransferError):
        transfers.reserve_cleanup("later", str(batch), "revision-one")


def test_empty_actors_only_and_other_batches_untouched(cleanup_env):
    service, _, batch, _ = cleanup_env
    for name in ["Alice", "演员😀", "With portrait"]:
        (batch / name).mkdir()
    (batch / "With portrait" / "folder.jpg").write_bytes(b"portrait")
    (batch / "root-file").write_bytes(b"keep")
    sibling = batch.parent / "other-batch" / "Alice"
    sibling.mkdir(parents=True)
    target = batch.parent / "nas" / "演员😀"
    target.mkdir(parents=True)
    result = finish(cleanup_env)
    assert result["status"] == "completed", result["message"]
    assert result["deleted_count"] == 2 and result["kept_count"] == 1
    assert result["checked_count"] == result["total_count"] == 3
    assert {item["name"] for item in result["items"] if item["status"] == "deleted"} == {"Alice", "演员😀"}
    assert batch.is_dir() and sibling.is_dir() and target.is_dir()
    assert (batch / "root-file").read_bytes() == b"keep"
    assert service.get_task()["id"] == result["id"]


@pytest.mark.parametrize("content", ["folder.jpg", "movie.nfo", ".hidden", "zero", "empty-subdir", "system", "ads"])
def test_strictly_nonempty_content_is_kept(cleanup_env, content):
    from tests.test_transfer import _add_named_stream
    _, _, batch, _ = cleanup_env
    actor = batch / "Actor"
    actor.mkdir()
    if content == "ads":
        _add_named_stream(actor, b"")
    elif content == "empty-subdir":
        (actor / content).mkdir()
    else:
        (actor / content).write_bytes(b"")
        if content == "system":
            import ctypes
            assert ctypes.windll.kernel32.SetFileAttributesW(str(actor / content), 6)
    result = finish(cleanup_env)
    assert result["status"] == "completed", result["message"]
    assert result["deleted_count"] == 0 and result["kept_count"] == 1
    assert result["items"][0]["status"] == "not_empty"
    assert actor.is_dir()


def test_hidden_actor_and_reparse_point_are_skipped(cleanup_env):
    _, _, batch, _ = cleanup_env
    hidden = batch / ".hidden-actor"
    hidden.mkdir()
    outside = batch.parent / "outside"
    outside.mkdir()
    link = batch / "Actor-link"
    subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(outside)],
                   check=True, capture_output=True)
    try:
        result = finish(cleanup_env)
        assert result["deleted_count"] == 0
        assert result["skipped_count"] == 2
        assert hidden.is_dir() and link.exists() and outside.is_dir()
    finally:
        link.rmdir()  # remove only this known disposable junction


def test_root_enumeration_failure_never_starts_deletion(cleanup_env, monkeypatch):
    import filmpaw_server.cleanup as cleanup
    _, _, batch, _ = cleanup_env
    (batch / "Actor").mkdir()
    original = cleanup.os.scandir
    class BrokenIterator:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def __iter__(self):
            with original(batch) as entries:
                yield next(entries)
            raise OSError("connection lost halfway through root enumeration")
    monkeypatch.setattr(cleanup.os, "scandir", lambda path: BrokenIterator())
    result = finish(cleanup_env)
    assert result["status"] == "failed"
    assert result["deleted_count"] == 0 and (batch / "Actor").is_dir()


def test_individual_failure_keeps_failed_actor_and_continues(cleanup_env, monkeypatch):
    import filmpaw_server.cleanup as cleanup
    _, _, batch, _ = cleanup_env
    (batch / "Denied").mkdir()
    (batch / "Empty").mkdir()
    original = cleanup._remove_empty
    def deny(root, candidate, checkpoint):
        if candidate["name"] == "Denied":
            raise PermissionError("permission denied")
        return original(root, candidate, checkpoint)
    monkeypatch.setattr(cleanup, "_remove_empty", deny)
    result = finish(cleanup_env)
    assert result["status"] == "completed"
    assert result["failed_count"] == 1 and result["deleted_count"] == 1
    assert (batch / "Denied").is_dir() and not (batch / "Empty").exists()


def test_context_revision_a_b_a_and_root_replacement_are_rejected(cleanup_env):
    service, _, batch, db = cleanup_env
    context = service.create_context(str(batch))
    db.execute("UPDATE settings SET value='revision-after-a-b-a' WHERE key='last_local_dir_revision'")
    db.commit()
    with pytest.raises(TransferError) as rejected:
        service.submit(context["id"], "stale")
    assert rejected.value.status_code == 409
    fresh = service.create_context(str(batch))
    batch.rename(batch.with_name("old-batch"))
    batch.mkdir()
    (batch / "Actor").mkdir()
    result = service.submit(fresh["id"], "replaced-root")
    result = finish(cleanup_env, result)
    assert result["status"] == "failed" and result["deleted_count"] == 0
    assert (batch / "Actor").exists()


@pytest.mark.parametrize("bad", ["relative", "parent", "other", "root"])
def test_context_cannot_authorize_arbitrary_paths(cleanup_env, bad):
    service, _, batch, _ = cleanup_env
    values = {"relative": "batch", "parent": str(batch.parent),
              "other": str(batch / "other"), "root": batch.anchor}
    with pytest.raises(TransferError):
        service.create_context(values[bad])


def test_legacy_settings_produce_nonempty_revision_without_writing(cleanup_env):
    service, _, batch, db = cleanup_env
    db.execute("DELETE FROM settings WHERE key='last_local_dir_revision'")
    db.commit()
    result = service.create_context(str(batch))
    assert result["batch_revision"]
    assert db.execute("SELECT value FROM settings WHERE key='last_local_dir_revision'").fetchone() is None


def test_idempotent_replay_never_deletes_recreated_actor(cleanup_env):
    service, _, batch, _ = cleanup_env
    (batch / "Actor").mkdir()
    task = start(cleanup_env)
    result = finish(cleanup_env, task)
    (batch / "Actor").mkdir()
    repeated = service.submit(task["context_id"], task["request_id"])
    assert repeated == result and (batch / "Actor").exists()
    with pytest.raises(TransferError):
        service.submit(task["context_id"], "new-request")
    new_context = service.create_context(str(batch))
    with pytest.raises(TransferError):
        service.submit(new_context["id"], task["request_id"])


def test_cleanup_reservation_blocks_transfer_another_cleanup_and_exit(cleanup_env):
    service, transfers, batch, _ = cleanup_env
    task = start(cleanup_env)
    with pytest.raises(TransferError, match="清理"):
        transfers.submit("unknown", ["MOV-001"], "transfer")
    other = service.create_context(str(batch))
    with pytest.raises(TransferError):
        service.submit(other["id"], "another-cleanup")
    assert transfers.prepare_exit() == {"safe": False, "task_id": task["id"], "operation": "cleanup"}
    finish(cleanup_env, task)
    assert transfers.prepare_exit() == {"safe": True, "task_id": None}
    with pytest.raises(TransferError):
        service.submit(other["id"], "after-exit")


def test_transfer_reservation_blocks_cleanup(cleanup_env):
    service, _, batch, db = cleanup_env
    context = service.create_context(str(batch))
    db.execute("INSERT INTO archive_tasks(id,request_id,payload) VALUES ('active','transfer-request',?)",
               (json.dumps({"id": "active", "status": "queued"}),))
    db.commit()
    with pytest.raises(TransferError):
        service.submit(context["id"], "cleanup")


def test_new_file_before_native_delete_is_kept(cleanup_env, monkeypatch):
    service, _, batch, _ = cleanup_env
    actor = batch / "Actor"
    actor.mkdir()
    def add(stage, task, item):
        if stage == "before_delete":
            (actor / "new.nfo").write_bytes(b"new")
    monkeypatch.setattr(service, "_checkpoint", add)
    result = finish(cleanup_env)
    assert result["deleted_count"] == 0 and result["kept_count"] == 1
    assert (actor / "new.nfo").read_bytes() == b"new"


def test_ads_added_before_native_delete_is_kept(cleanup_env, monkeypatch):
    from tests.test_transfer import _add_named_stream
    service, _, batch, _ = cleanup_env
    actor = batch / "Actor"
    actor.mkdir()
    def add(stage, task, item):
        if stage == "before_delete":
            _add_named_stream(actor)
    monkeypatch.setattr(service, "_checkpoint", add)
    result = finish(cleanup_env)
    assert result["deleted_count"] == 0 and result["kept_count"] == 1
    assert Path(str(actor) + ":filmpaw-test").exists()


@pytest.mark.parametrize("field", ["identity", "path"])
def test_actual_delete_handle_identity_and_path_are_validated(cleanup_env, monkeypatch, field):
    import filmpaw_server.cleanup as cleanup
    _, _, batch, _ = cleanup_env
    actor = batch / "Actor"
    actor.mkdir()
    original = cleanup._native_info
    hits = 0
    def changed_handle(handle):
        nonlocal hits
        info = original(handle)
        if Path(info["path"]).name == "Actor":
            hits += 1
            if hits == 2:  # snapshot capture, then the actual DELETE handle
                info[field] = ["wrong", "identity"] if field == "identity" else str(batch.parent / "outside" / "Actor")
        return info
    monkeypatch.setattr(cleanup, "_native_info", changed_handle)
    result = finish(cleanup_env)
    assert hits == 2 and result["failed_count"] == 1
    assert result["deleted_count"] == 0 and actor.is_dir()


def test_get_task_and_close_have_no_filesystem_replay(cleanup_env, monkeypatch):
    import filmpaw_server.cleanup as cleanup
    service, transfers, batch, _ = cleanup_env
    task = finish(cleanup_env)
    monkeypatch.setattr(cleanup, "_snapshot", lambda *args: pytest.fail("status lookup must be read-only"))
    assert service.get_task(task["id"]) == task
    with pytest.raises(TransferError) as missing:
        service.get_task("unknown")
    assert missing.value.status_code == 404
    service.close()
    with pytest.raises(TransferError):
        service.create_context(str(batch))
    with pytest.raises(TransferError):
        transfers.submit("unknown", ["MOV-001"], "after-close")


def test_status_and_exit_stay_responsive_while_cleanup_io_blocks(cleanup_env, monkeypatch):
    service, transfers, batch, _ = cleanup_env
    (batch / "Actor").mkdir()
    entered, release = threading.Event(), threading.Event()
    def pause(stage, task, item):
        if stage == "before_delete":
            entered.set()
            assert release.wait(5)
    monkeypatch.setattr(service, "_checkpoint", pause)
    service._auto_start = True
    task = start(cleanup_env)
    try:
        assert entered.wait(5)
        assert service.get_task(task["id"])["status"] == "running"
        assert not transfers.prepare_exit()["safe"]
        assert transfers.db_lock.acquire(timeout=0.1)
        transfers.db_lock.release()
    finally:
        release.set()
        service.close()
    assert service.get_task(task["id"])["status"] == "completed"


@pytest.mark.parametrize("name,attrs", [("Hidden actor", 2), ("System actor", 4)])
def test_windows_hidden_and_system_actors_are_skipped(cleanup_env, name, attrs):
    import ctypes
    batch = cleanup_env[2]
    actor = batch / name
    actor.mkdir()
    assert ctypes.windll.kernel32.SetFileAttributesW(str(actor), attrs)
    result = finish(cleanup_env)
    assert result["skipped_count"] == 1 and result["deleted_count"] == 0
    assert actor.is_dir()


def test_root_and_ancestor_guard_reject_real_rename_during_delete(cleanup_env, monkeypatch):
    service, _, batch, _ = cleanup_env
    (batch / "Actor").mkdir()
    outside = batch.parent / "outside"
    outside.mkdir()
    (outside / "keep").write_bytes(b"outside must survive")
    attempts = []
    def replace(stage, task, item):
        if stage == "before_delete":
            for directory in [batch, batch.parent]:
                with pytest.raises(PermissionError):
                    directory.rename(directory.with_name(directory.name + "-moved"))
                attempts.append(directory)
    monkeypatch.setattr(service, "_checkpoint", replace)
    result = finish(cleanup_env)
    assert result["deleted_count"] == 1 and len(attempts) == 2
    assert batch.is_dir() and (outside / "keep").read_bytes() == b"outside must survive"


def test_ads_information_error_preserves_empty_actor(cleanup_env, monkeypatch):
    import filmpaw_server.cleanup as cleanup
    actor = cleanup_env[2] / "Actor"
    actor.mkdir()
    def fail(handle, path):
        raise OSError("filesystem does not support stream metadata")
    monkeypatch.setattr(cleanup, "_check_named_streams", fail)
    result = finish(cleanup_env)
    assert result["failed_count"] == 1 and result["deleted_count"] == 0
    assert actor.is_dir()


def test_root_actual_handle_identity_is_checked_again_before_work(cleanup_env, monkeypatch):
    import filmpaw_server.cleanup as cleanup
    actor = cleanup_env[2] / "Actor"
    actor.mkdir()
    task = start(cleanup_env)
    original = cleanup._native_info
    def changed(handle):
        info = original(handle)
        info["identity"] = ["different-volume", "different-directory"]
        return info
    monkeypatch.setattr(cleanup, "_native_info", changed)
    result = finish(cleanup_env, task)
    assert result["status"] == "failed" and result["deleted_count"] == 0
    assert actor.is_dir()


def test_snapshot_rejects_handle_with_final_parent_outside_batch(cleanup_env, monkeypatch):
    import filmpaw_server.cleanup as cleanup
    _, _, batch, _ = cleanup_env
    actor = batch / "Actor"
    actor.mkdir()
    original = cleanup._native_info
    def outside(handle):
        info = original(handle)
        if Path(info["path"]).name == "Actor":
            info["path"] = str(batch.parent / "outside" / "Actor")
        return info
    monkeypatch.setattr(cleanup, "_native_info", outside)
    result = finish(cleanup_env)
    assert result["failed_count"] == 1 and result["deleted_count"] == 0
    assert actor.is_dir()


def test_accepted_task_keeps_original_batch_after_settings_change(cleanup_env):
    _, _, batch, db = cleanup_env
    (batch / "Actor").mkdir()
    other = batch.parent / "other-batch"
    (other / "Actor").mkdir(parents=True)
    task = start(cleanup_env)
    db.execute("UPDATE settings SET value=? WHERE key='last_local_dir'", (str(other),))
    db.execute("UPDATE settings SET value='new-revision' WHERE key='last_local_dir_revision'")
    db.commit()
    result = finish(cleanup_env, task)
    assert result["deleted_count"] == 1 and not (batch / "Actor").exists()
    assert (other / "Actor").is_dir()


def test_consumed_context_stays_invalid_after_result_eviction(cleanup_env, monkeypatch):
    import filmpaw_server.cleanup as cleanup
    service, _, batch, _ = cleanup_env
    monkeypatch.setattr(cleanup, "CACHE_LIMIT", 2)
    tasks = [finish(cleanup_env, start(cleanup_env, request_id=f"request-{i}")) for i in range(3)]
    first = tasks[0]
    (batch / "Actor").mkdir()
    with pytest.raises(TransferError) as expired:
        service.submit(first["context_id"], first["request_id"])
    assert expired.value.status_code == 409
    with pytest.raises(TransferError) as missing:
        service.get_task(first["id"])
    assert missing.value.status_code == 404
    assert (batch / "Actor").is_dir()


def test_new_session_never_recovers_or_accepts_old_context(cleanup_env):
    from filmpaw_server.cleanup import CleanupService
    _, _, batch, db = cleanup_env
    old = finish(cleanup_env)
    (batch / "Actor").mkdir()
    transfers = TransferService(db, threading.RLock(), auto_start=False)
    fresh = CleanupService(transfers, auto_start=False)
    try:
        assert fresh.get_task() is None
        with pytest.raises(TransferError):
            fresh.submit(old["context_id"], old["request_id"])
        assert (batch / "Actor").is_dir()
    finally:
        fresh.close()
        transfers.close()


def test_context_reads_have_nonblocking_gate_and_recover(cleanup_env, monkeypatch):
    import filmpaw_server.cleanup as cleanup
    service, _, batch, _ = cleanup_env
    entered, release = threading.Event(), threading.Event()
    original = cleanup._native_info
    result = []
    def pause(handle):
        entered.set()
        assert release.wait(5)
        return original(handle)
    monkeypatch.setattr(cleanup, "_native_info", pause)
    worker = threading.Thread(target=lambda: result.append(service.create_context(str(batch))))
    worker.start()
    try:
        assert entered.wait(5)
        with pytest.raises(TransferError) as busy:
            service.create_context(str(batch))
        assert busy.value.status_code == 503
    finally:
        release.set()
        worker.join(5)
    assert result and not worker.is_alive()
    assert service.create_context(str(batch))["id"] != result[0]["id"]


def test_close_waits_for_cleanup_and_closes_shared_admission(cleanup_env, monkeypatch):
    service, transfers, batch, _ = cleanup_env
    (batch / "Actor").mkdir()
    entered, release, closed = threading.Event(), threading.Event(), threading.Event()
    def pause(stage, task, item):
        if stage == "before_delete":
            entered.set()
            assert release.wait(5)
    monkeypatch.setattr(service, "_checkpoint", pause)
    service._auto_start = True
    task = start(cleanup_env)
    assert entered.wait(5)
    def close():
        service.close()
        closed.set()
    closer = threading.Thread(target=close)
    closer.start()
    try:
        assert not closed.wait(0.05)
        with pytest.raises(TransferError, match="退出"):
            transfers.submit("unknown", ["MOV-001"], "during-close")
    finally:
        release.set()
        closer.join(5)
    assert closed.is_set()
    assert service.get_task(task["id"])["status"] == "completed"
    assert transfers.prepare_exit()["safe"]


def test_worker_start_failure_can_close_without_joining_unstarted_thread(cleanup_env, monkeypatch):
    service, transfers, batch, _ = cleanup_env
    actor = batch / "Actor"
    actor.mkdir()
    service._auto_start = True
    def fail_start(thread):
        raise RuntimeError("cannot start a new thread")
    with monkeypatch.context() as patch:
        patch.setattr(threading.Thread, "start", fail_start)
        task = start(cleanup_env)
    assert task["status"] == "failed" and task["deleted_count"] == 0
    assert actor.is_dir()
    assert transfers.prepare_exit() == {"safe": True, "task_id": None}
    try:
        service.close()  # real Thread.join rejects a Thread whose start failed
        transfers.close()
    finally:
        # Keep the RED test's fixture teardown independent of its close failure.
        service._worker = None


def test_worker_start_failure_releases_reservation_and_next_task_runs(cleanup_env, monkeypatch):
    service, _, batch, _ = cleanup_env
    actor = batch / "Actor"
    actor.mkdir()
    service._auto_start = True
    def fail_start(thread):
        raise RuntimeError("cannot start a new thread")
    with monkeypatch.context() as patch:
        patch.setattr(threading.Thread, "start", fail_start)
        failed = start(cleanup_env)
    assert failed["status"] == "failed" and actor.is_dir()
    retry = start(cleanup_env, request_id="new-explicit-cleanup")
    service._worker.join(timeout=5)
    result = service.get_task(retry["id"])
    assert result["status"] == "completed" and result["deleted_count"] == 1
    assert not actor.exists()
    actor.mkdir()
    assert service.submit(failed["context_id"], failed["request_id"]) == failed
    assert actor.is_dir(), "replaying the failed request must never start another cleanup"
