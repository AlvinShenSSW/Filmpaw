"""Operation history must survive restart without inventing successful deletes."""

import sqlite3
import threading

import pytest

from filmpaw_server.audit import AuditError, OperationLog
from filmpaw_server.db import ARCHIVE_DDL, DDL, connect


TASK = {"id": "task-a", "performer_name": "Actor", "source_path": "batch/Actor",
        "target_path": "nas/Actor"}


def test_intent_and_confirmed_result_survive_restart_and_are_idempotent(tmp_path):
    path = tmp_path / "history.db"
    conn = connect(path)
    log = OperationLog(conn, threading.RLock())
    calls = []
    log.perform(TASK, "delete_source_directory", lambda: calls.append("deleted"), item_name="MOV-1")
    log.record(TASK, "delete_source_directory", "succeeded", item_name="MOV-1")
    assert calls == ["deleted"]
    conn.close()
    conn = connect(path)
    try:
        rows = OperationLog(conn, threading.RLock()).list()["items"]
        assert [row["outcome"] for row in rows] == ["succeeded", "started"]
        assert rows[0]["operation_id"] == rows[1]["operation_id"]
        assert rows[0]["performer_name"] == "Actor"
    finally:
        conn.close()


def test_interrupted_intent_is_marked_uncertain_without_replaying_action(db):
    log = OperationLog(db, threading.RLock())
    log.record(TASK, "delete_empty_actor", "started", kind="cleanup", item_name="Actor")
    log.recover_pending()
    log.recover_pending()
    rows = log.list()["items"]
    assert [row["outcome"] for row in rows] == ["needs_review", "started"]
    assert rows[0]["operation_id"] == rows[1]["operation_id"]
    assert not any(row["outcome"] == "succeeded" for row in rows)


def test_failed_intent_prevents_filesystem_action_and_freezes_log(db):
    log = OperationLog(db, threading.RLock())
    db.execute("CREATE TRIGGER break_log BEFORE INSERT ON operation_events BEGIN SELECT RAISE(FAIL,'disk full'); END")
    db.commit()
    calls = []
    with pytest.raises(AuditError):
        log.perform(TASK, "delete_source_file", lambda: calls.append("deleted"))
    assert calls == []
    assert log.failed
    db.execute("DROP TRIGGER break_log")
    db.commit()
    with pytest.raises(AuditError):
        log.perform(TASK, "delete_source_file", lambda: calls.append("deleted"))
    assert calls == []


def test_success_receipt_failure_never_fabricates_confirmed_delete(db):
    log = OperationLog(db, threading.RLock())
    calls = []
    db.execute("CREATE TRIGGER break_result BEFORE INSERT ON operation_events WHEN NEW.outcome='succeeded' BEGIN SELECT RAISE(FAIL,'disk full'); END")
    db.commit()
    with pytest.raises(AuditError):
        log.perform(TASK, "delete_source_file", lambda: calls.append("deleted"))
    assert calls == ["deleted"]
    assert [row["outcome"] for row in log.list()["items"]] == ["started"]
    db.execute("DROP TRIGGER break_result")
    db.commit()
    restarted = OperationLog(db, threading.RLock())
    restarted.recover_pending()
    assert [row["outcome"] for row in restarted.list()["items"]] == ["needs_review", "started"]


def test_action_failure_is_recorded_and_does_not_claim_success(db):
    log = OperationLog(db, threading.RLock())
    def fail():
        raise OSError("NAS unavailable")
    with pytest.raises(OSError, match="NAS unavailable"):
        log.perform(TASK, "commit_movie", fail, item_name="MOV-1")
    rows = log.list()["items"]
    assert [row["outcome"] for row in rows] == ["failed", "started"]
    assert "NAS unavailable" in rows[0]["message"]


def test_cursor_and_filters_do_not_repeat_rows_when_new_events_arrive(db):
    log = OperationLog(db, threading.RLock())
    for name in ["A", "B", "C"]:
        log.record(TASK, "delete_source_directory", "succeeded", item_name=name)
    first = log.list(limit=2, task_id=TASK["id"], outcome="succeeded")
    log.record({**TASK, "id": "task-b"}, "delete_empty_actor", "succeeded", item_name="D", kind="cleanup")
    second = log.list(limit=2, before_id=first["next_cursor"], task_id=TASK["id"], outcome="succeeded")
    assert [row["item_name"] for row in first["items"] + second["items"]] == ["C", "B", "A"]
    assert second["next_cursor"] is None
    assert len(log.list(action="delete_empty_actor")["items"]) == 1


def _schema_three_library(path):
    # Build the old schema directly. Relabeling a current library as v3 leaves
    # newer workflow tables behind and does not represent an upgrade fixture.
    conn = sqlite3.connect(path)
    conn.executescript(DDL)
    conn.execute(ARCHIVE_DDL)
    conn.execute("INSERT INTO schema_version(version) VALUES (3)")
    return conn


def test_schema_three_migration_preserves_existing_library_and_tasks(tmp_path):
    path = tmp_path / "old.db"
    conn = _schema_three_library(path)
    conn.execute("INSERT INTO settings(key,value) VALUES ('last_local_dir','approved-batch')")
    conn.execute("INSERT INTO archive_tasks(id,request_id,payload) VALUES ('old-task','old-request','{\"status\":\"completed\"}')")
    conn.commit()
    conn.close()
    conn = connect(path)
    try:
        assert conn.execute("SELECT value FROM settings WHERE key='last_local_dir'").fetchone()[0] == "approved-batch"
        assert conn.execute("SELECT payload FROM archive_tasks WHERE id='old-task'").fetchone()[0] == '{"status":"completed"}'
        assert conn.execute("SELECT public_payload FROM archive_tasks WHERE id='old-task'").fetchone()[0] == '{"status": "completed", "items": []}'
        assert OperationLog(conn, threading.RLock()).list()["items"] == []
    finally:
        conn.close()


def test_failed_schema_four_migration_rolls_back_event_table(tmp_path, monkeypatch):
    import filmpaw_server.db as module
    path = tmp_path / "old.db"
    conn = _schema_three_library(path)
    conn.commit()
    conn.close()
    original = module._MIGRATIONS[3]
    def crash(conn):
        original(conn)
        raise RuntimeError("interrupted migration")
    monkeypatch.setitem(module._MIGRATIONS, 3, crash)
    with pytest.raises(RuntimeError, match="interrupted migration"):
        connect(path)
    conn = sqlite3.connect(path)
    try:
        assert conn.execute("SELECT version FROM schema_version").fetchone()[0] == 3
        assert conn.execute("SELECT name FROM sqlite_master WHERE name='operation_events'").fetchone() is None
        assert "public_payload" not in [row[1] for row in conn.execute("PRAGMA table_info(archive_tasks)")]
    finally:
        conn.close()
