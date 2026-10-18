"""Actor concurrency and ownership are verified only with disposable trees."""

import threading
import time
from pathlib import Path

import pytest

from filmpaw_server.transfer import TransferError, TransferService
from tests.test_transfer import movie


@pytest.fixture()
def multi(db, tmp_path):
    batch, nas = tmp_path / "batch", tmp_path / "nas"
    batch.mkdir()
    nas.mkdir()
    db.execute("INSERT INTO settings VALUES ('last_local_dir',?)", (str(batch),))
    db.execute("INSERT INTO sources(id,unc_path,label) VALUES (1,?,'NAS')", (str(nas),))
    for name in ["Actor-A", "Actor-B", "Actor-C", "Actor-D"]:
        (batch / name).mkdir()
        (nas / name).mkdir()
        movie(batch / name)
        db.execute("INSERT INTO performers(id,name,name_norm,source_id,folder_name,unc_path,first_seen_at,last_seen_at)"
                   " VALUES (?,?,?,1,?,?,'now','now')", (name, name, name, name, str(nas / name)))
    db.commit()
    service = TransferService(db, threading.RLock(), auto_start=False)
    yield service, batch, nas, db
    service.close()


def context(multi, name):
    return multi[0].create_context(str(multi[1]), name, name)


def submit(multi, name, request=None):
    ctx = context(multi, name)
    return multi[0].submit(ctx["id"], ["MOV-001"], request or name)


def wait_until(check):
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if check():
            return
        time.sleep(0.01)
    raise AssertionError("condition did not become true")


def test_three_actors_really_run_together_and_fourth_waits(multi, monkeypatch):
    service, batch, nas, _ = multi
    releases = {name: threading.Event() for name in ["Actor-A", "Actor-B", "Actor-C", "Actor-D"]}
    entered = set()
    def pause(stage, task, item):
        if stage == "before_copy":
            entered.add(task["subdir"])
            assert releases[task["subdir"]].wait(10)
    monkeypatch.setattr(service, "_checkpoint", pause)
    service._auto_start = True
    try:
        tasks = [submit(multi, name) for name in releases]
        wait_until(lambda: len(entered) == 3)
        assert entered == {"Actor-A", "Actor-B", "Actor-C"}
        assert service.get_task(tasks[3]["id"])["status"] == "queued"
        listing = service.list_tasks()
        assert listing["busy"] and listing["concurrency"] == 3
        assert len(listing["active"]) == 4
        releases["Actor-A"].set()
        wait_until(lambda: "Actor-D" in entered)
        assert all(service.get_task(task["id"])["status"] == "running" for task in tasks[1:3])
        for release in releases.values():
            release.set()
        wait_until(lambda: not service.list_tasks()["busy"])
    finally:
        for release in releases.values():
            release.set()
        service.close()
    assert all(service.get_task(task["id"])["status"] == "completed" for task in tasks)
    assert all((nas / name / "MOV-001" / "video.mp4").exists() for name in releases)
    assert all(not (batch / name / "MOV-001").exists() for name in releases)


def test_stopping_actor_a_does_not_stop_actor_b(multi, monkeypatch):
    service = multi[0]
    entered, release = set(), threading.Event()
    def pause(stage, task, item):
        if stage == "before_copy":
            entered.add(task["id"])
            assert release.wait(10)
    monkeypatch.setattr(service, "_checkpoint", pause)
    service._auto_start = True
    try:
        a, b = submit(multi, "Actor-A"), submit(multi, "Actor-B")
        wait_until(lambda: len(entered) == 2)
        service.stop(a["id"])
        release.set()
        wait_until(lambda: not service.list_tasks()["busy"])
        assert service.get_task(a["id"])["status"] == "stopped"
        assert service.get_task(b["id"])["status"] == "completed"
    finally:
        release.set()


def test_queued_tasks_own_both_actor_paths(multi):
    service, batch, nas, db = multi
    first = submit(multi, "Actor-A")
    with pytest.raises(TransferError, match="目录|占用|任务"):
        submit(multi, "Actor-A", "repeat-actor")
    db.execute("UPDATE performers SET unc_path=? WHERE id='Actor-B'", ((nas / "Actor-A").as_posix(),))
    db.commit()
    with pytest.raises(TransferError):
        submit(multi, "Actor-B")
    assert service.get_task(first["id"])["status"] == "queued"
    assert (batch / "Actor-A" / "MOV-001").exists()


@pytest.mark.parametrize("cross_path", ["same", "child"])
def test_cross_role_and_nested_actor_paths_conflict(multi, cross_path):
    _, batch, _, db = multi
    submit(multi, "Actor-A")
    target = batch / "Actor-A"
    if cross_path == "child":
        target = target / "MOV-001"
    db.execute("UPDATE performers SET unc_path=? WHERE id='Actor-B'", (str(target),))
    db.commit()
    with pytest.raises(TransferError):
        submit(multi, "Actor-B")


def test_stop_all_only_stops_the_explicit_task_ids(multi):
    service = multi[0]
    a, b = submit(multi, "Actor-A"), submit(multi, "Actor-B")
    assert service.stop_all([a["id"]])["task_ids"] == [a["id"]]
    c = submit(multi, "Actor-C")
    service.stop_all([a["id"]])
    assert service.get_task(a["id"])["status"] == "stopped"
    assert service.get_task(b["id"])["status"] == "queued"
    assert service.get_task(c["id"])["status"] == "queued"


def test_exact_request_lookup_and_history_do_not_hide_active_tasks(multi):
    service = multi[0]
    a, b, c = [submit(multi, name) for name in ["Actor-A", "Actor-B", "Actor-C"]]
    service.stop(a["id"])
    service.stop(b["id"])
    first = service.list_tasks(limit=1)
    assert [task["id"] for task in first["active"]] == [c["id"]]
    assert [task["id"] for task in first["history"]] == [b["id"]]
    assert first["next_cursor"] is not None
    second = service.list_tasks(before_id=first["next_cursor"], limit=1)
    assert [task["id"] for task in second["history"]] == [a["id"]]
    assert service.get_by_request("Actor-A")["id"] == a["id"]
    assert service.get_by_request("unaccepted-request") is None


def test_failed_task_does_not_stop_other_actor(multi):
    service, _, nas, _ = multi
    (nas / "Actor-A" / "MOV-001").mkdir()
    a, b = submit(multi, "Actor-A"), submit(multi, "Actor-B")
    service._run(a["id"])
    service._run(b["id"])
    assert service.get_task(a["id"])["status"] == "failed"
    assert service.get_task(b["id"])["status"] == "completed"


def test_worker_start_failure_releases_slot_and_next_actor_still_runs(multi, monkeypatch):
    service = multi[0]
    original = threading.Thread.start
    failed = False
    def start(thread):
        nonlocal failed
        if thread.name.startswith("archive-") and thread.name != "archive-scheduler" and not failed:
            failed = True
            raise RuntimeError("fixture cannot start worker")
        original(thread)
    monkeypatch.setattr(threading.Thread, "start", start)
    service._auto_start = True
    a, b = submit(multi, "Actor-A"), submit(multi, "Actor-B")
    wait_until(lambda: not service.list_tasks()["busy"])
    assert service.get_task(a["id"])["status"] == "failed"
    assert service.get_task(b["id"])["status"] == "completed"
    assert (multi[1] / "Actor-A" / "MOV-001").exists()
    assert not (multi[2] / "Actor-A" / "MOV-001").exists()
    # Retrying the actor after the failed thread does not inherit its slot.
    again = submit(multi, "Actor-A", "new-after-start-failure")
    wait_until(lambda: not service.list_tasks()["busy"])
    assert service.get_task(again["id"])["status"] == "completed"
    service.close()  # never join an unstarted Thread


def test_terminal_worker_keeps_slot_paths_and_exit_busy_until_actual_return(multi, monkeypatch):
    service = multi[0]
    original = service._run
    release, terminal = threading.Event(), threading.Event()
    def finalizing(task_id):
        original(task_id)
        terminal.set()
        assert release.wait(10)
    monkeypatch.setattr(service, "_run", finalizing)
    service._auto_start = True
    try:
        a = submit(multi, "Actor-A")
        assert terminal.wait(10)
        assert service.get_task(a["id"])["status"] == "completed"
        assert service.list_tasks()["active"][0]["id"] == a["id"]
        assert service.prepare_exit() == {"safe": False, "task_id": a["id"]}
        movie(multi[1] / "Actor-A")
        with pytest.raises(TransferError):
            submit(multi, "Actor-A", "during-finalization")
    finally:
        release.set()
    wait_until(lambda: not service.list_tasks()["busy"])


@pytest.mark.parametrize("action,outcome", [
    ("commit_movie", "started"), ("commit_movie", "succeeded"),
    ("delete_source_file", "started"), ("delete_source_file", "succeeded"),
])
def test_log_failure_freezes_all_tasks_and_recovery_never_replays_actions(multi, monkeypatch, action, outcome):
    service, batch, nas, db = multi
    releases = {name: threading.Event() for name in ["Actor-A", "Actor-B", "Actor-C"]}
    entered = set()
    def pause(stage, task, item):
        if stage == "before_copy":
            entered.add(task["subdir"])
            assert releases[task["subdir"]].wait(10)
    monkeypatch.setattr(service, "_checkpoint", pause)
    service._auto_start = True
    try:
        a, b, c, d = [submit(multi, name) for name in ["Actor-A", "Actor-B", "Actor-C", "Actor-D"]]
        wait_until(lambda: len(entered) == 3)
        assert service.get_task(d["id"])["status"] == "queued"
        with service.db_lock:
            db.execute(f"CREATE TRIGGER fail_receipt BEFORE INSERT ON operation_events "
                f"WHEN NEW.task_id='{a['id']}' AND NEW.action='{action}' AND NEW.outcome='{outcome}' "
                "BEGIN SELECT RAISE(FAIL,'fixture receipt storage unavailable'); END")
            db.commit()
        releases["Actor-A"].set()
        wait_until(lambda: service.get_task(a["id"])["status"] == "needs_review")
        assert service.list_tasks()["blocked_reason"]
        assert service.prepare_exit()["safe"] is False, "Actor-B and Actor-C still have real workers"
        with pytest.raises(TransferError):
            submit(multi, "Actor-D", "new-after-fault")
        releases["Actor-B"].set()
        releases["Actor-C"].set()
        wait_until(lambda: not service.list_tasks()["busy"])
        assert entered == {"Actor-A", "Actor-B", "Actor-C"}, "queued Actor-D must never start"
        assert all(service.get_task(task["id"])["status"] == "needs_review" for task in [a, b, c, d])
        assert all((batch / name / "MOV-001").exists() for name in ["Actor-B", "Actor-C", "Actor-D"])
        assert all(not (nas / name / "MOV-001").exists() for name in ["Actor-B", "Actor-C", "Actor-D"])
        assert (batch / "Actor-A" / "MOV-001").exists()
        assert (nas / "Actor-A" / "MOV-001").exists() == (action != "commit_movie" or outcome != "started")
        assert service.prepare_exit()["safe"]
    finally:
        for event in releases.values():
            event.set()
        service.close()
    db.execute("DROP TRIGGER fail_receipt")
    db.commit()
    before = sorted(str(path.relative_to(batch)) for path in batch.rglob("*"))
    restored = TransferService(db, threading.RLock(), auto_start=False)
    restored.recover()
    try:
        assert sorted(str(path.relative_to(batch)) for path in batch.rglob("*")) == before
        assert not restored.list_tasks()["busy"]
        assert all(restored.get_task(task["id"])["status"] == "needs_review" for task in [a, b, c, d])
        events = restored.audit.list(task_id=a["id"], action=action)["items"]
        if outcome == "succeeded":
            assert any(row["outcome"] == "needs_review" for row in events)
            assert not any(row["outcome"] == "succeeded" for row in events)
    finally:
        restored.close()


def test_stop_all_unknown_id_is_atomic_and_terminal_ids_are_idempotent(multi):
    service = multi[0]
    a, b = submit(multi, "Actor-A"), submit(multi, "Actor-B")
    with pytest.raises(TransferError):
        service.stop_all([a["id"], "unknown", b["id"]])
    assert all(task["status"] == "queued" for task in service.list_tasks()["active"])
    for _ in range(2):
        assert service.stop_all([b["id"], a["id"], b["id"]]) == {"task_ids": [b["id"], a["id"]]}


def test_queue_is_bounded_at_one_hundred_and_cancel_releases_capacity(multi):
    service, batch, nas, db = multi
    tasks = []
    for number in range(101):
        name = f"Extra-{number}"
        (batch / name / "MOV-001").mkdir(parents=True)
        (nas / name).mkdir()
        db.execute("INSERT INTO performers(id,name,name_norm,source_id,folder_name,unc_path,first_seen_at,last_seen_at)"
                   " VALUES (?,?,?,1,?,?,'now','now')", (name, name, name, name, str(nas / name)))
    db.commit()
    for number in range(100):
        tasks.append(submit(multi, f"Extra-{number}"))
    with pytest.raises(TransferError, match="100"):
        submit(multi, "Extra-100")
    assert len(service.list_tasks(limit=1)["active"]) == 100
    service.stop(tasks[0]["id"])
    assert submit(multi, "Extra-100")["status"] == "queued"
