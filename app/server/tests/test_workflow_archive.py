"""Workflow archive (F02-P1): copy an approved package into a new NAS folder, keep the source.

Disposable folders only: tmp_path/attempt/out/<film> is the Taskpaw package, tmp_path/nas/Actor the
performer folder.
"""

import hashlib
import json
import os
import threading
from pathlib import Path

import pytest

from filmpaw_server.transfer import TransferError, TransferService, _directory

from tests.conftest import SESSION_TOKEN

FILM = "ABC-123"


@pytest.fixture()
def flow(db, tmp_path):
    out = tmp_path / "attempt" / "out"
    target = tmp_path / "nas" / "Actor"
    out.mkdir(parents=True)
    target.mkdir(parents=True)
    db.execute("INSERT INTO sources(id,unc_path,label) VALUES (1,?,'NAS')", (str(target.parent),))
    db.execute(
        "INSERT INTO performers(id,name,name_norm,source_id,folder_name,unc_path,first_seen_at,last_seen_at)"
        " VALUES ('actor','Actor','actor',1,'Actor',?,'now','now')",
        (str(target),),
    )
    db.commit()
    service = TransferService(db, threading.RLock(), auto_start=False)
    yield service, out, target, db
    service.close()


def package(out: Path, name: str = FILM) -> Path:
    root = out / name
    root.mkdir()
    (root / f"{name}-cd1.mp4").write_bytes(bytes(range(256)) * 40)
    (root / f"{name}-cd2.mp4").write_bytes(bytes(range(255, -1, -1)) * 40)
    for stem in (f"{name}-cd1", f"{name}-cd2"):
        (root / f"{stem}.nfo").write_text("<movie/>", encoding="utf-8")
        (root / f"{stem}.chs.srt").write_text("字幕", encoding="utf-8")
    for pic in ("poster.jpg", "thumb.jpg", "fanart.jpg", "folder.jpg"):
        (root / pic).write_bytes(pic.encode())
    return root


def files_of(root: Path) -> list[dict]:
    return [
        {
            "path": p.relative_to(root).as_posix(),
            "size": p.stat().st_size,
            "sha256": hashlib.sha256(p.read_bytes()).hexdigest(),
        }
        for p in sorted(root.rglob("*"))
        if p.is_file()
    ]


def expected_target(service, target: Path) -> dict:
    return service.workflow_target("actor", FILM)["expected_target"]


def create(flow, root: Path, files=None, operation_id="op-1", expected=None):
    service, _, target, _ = flow
    return service.create_workflow_task(
        operation_id,
        str(root),
        "actor",
        expected or expected_target(service, target),
        files if files is not None else files_of(root),
    )


def run(flow, task):
    flow[0]._run(task["id"])
    return flow[0].get_task(task["id"])


def test_package_is_copied_to_a_new_folder_and_the_source_is_kept(flow):
    service, out, target, _ = flow
    root = package(out)
    files = files_of(root)
    task = run(flow, create(flow, root))
    assert task["status"] == "completed", task["message"]
    copied = target / FILM
    assert files_of(copied) == files
    assert files_of(root) == files  # Taskpaw cleans its own source exactly (F02 C5/C6)
    receipt = task["receipt"]
    assert receipt["target_path"] == str(copied)
    assert receipt["directory_identity"] == _directory(copied)
    assert sorted(receipt["files"], key=lambda f: f["path"]) == files
    assert receipt["task_id"] == task["id"]


def test_target_proof_reports_identity_and_absence(flow):
    service, out, target, _ = flow
    proof = service.workflow_target("actor", FILM)
    assert proof["absent"] is True
    assert proof["expected_target"] == {"target_path": str(target), "directory_identity": _directory(target)}
    (target / FILM.lower()).mkdir()
    assert service.workflow_target("actor", FILM)["absent"] is False


def test_staging_leftover_does_not_count_as_present(flow):
    service, _, target, _ = flow
    (target / ".filmpaw-transfer-old-abc").mkdir()
    assert service.workflow_target("actor", FILM)["absent"] is True


def test_existing_target_case_insensitive_is_refused_before_copy(flow):
    service, out, target, _ = flow
    root = package(out)
    (target / FILM.lower()).mkdir()
    (target / FILM.lower() / "keep.txt").write_text("existing", encoding="utf-8")
    task = run(flow, create(flow, root))
    assert task["status"] == "failed" and task["conflicts"] == [FILM]
    assert (target / FILM.lower() / "keep.txt").read_text(encoding="utf-8") == "existing"
    assert [p.name for p in target.iterdir()] == [FILM.lower()]


@pytest.mark.parametrize("change", ["extra", "missing", "size"])
def test_file_set_must_equal_the_approved_manifest(flow, change):
    _, out, target, _ = flow
    root = package(out)
    files = files_of(root)
    if change == "extra":
        (root / "trailer.mp4").write_bytes(b"not approved")
    elif change == "missing":
        files.append({"path": "extrafanart/1.jpg", "size": 1, "sha256": "0" * 64})
    else:
        (root / "poster.jpg").write_bytes(b"poster.jpg+")
    task = run(flow, create(flow, root, files=files))
    assert task["status"] == "failed"
    assert not (target / FILM).exists()


def test_content_hash_mismatch_is_never_committed(flow):
    _, out, target, _ = flow
    root = package(out)
    files = files_of(root)
    (root / "poster.jpg").write_bytes(b"POSTER.JPG")  # same size, different bytes
    task = run(flow, create(flow, root, files=files))
    assert task["status"] == "failed"
    assert not (target / FILM).exists()
    assert not [p for p in target.iterdir() if not p.name.startswith(".filmpaw-transfer-")]


def test_target_changed_since_approval_is_refused(flow):
    service, out, target, _ = flow
    root = package(out)
    stale = expected_target(service, target)
    os.rename(target, target.with_name("Actor-old"))
    target.mkdir()
    with pytest.raises(TransferError) as e:
        create(flow, root, expected=stale)
    assert e.value.status_code == 412  # the approval's proof no longer holds: Taskpaw fails it
    other = dict(stale, target_path=str(target.with_name("Elsewhere")))
    with pytest.raises(TransferError):
        create(flow, root, expected=other)


def test_same_operation_is_idempotent_and_differing_reuse_is_refused(flow):
    service, out, target, _ = flow
    root = package(out)
    first = create(flow, root)
    assert create(flow, root)["id"] == first["id"]
    with pytest.raises(TransferError) as e:
        create(flow, root, files=files_of(root)[1:])
    assert e.value.status_code == 412  # the approval's proof no longer holds: Taskpaw fails it
    assert service.get_workflow_task("unknown-op") is None
    assert service.get_workflow_task("op-1")["id"] == first["id"]


class PowerLoss(BaseException):
    pass


def test_crash_after_commit_reconciles_to_completed_with_receipt(flow, monkeypatch):
    service, out, target, db = flow
    root = package(out)

    def crash(stage, task, item):
        if stage == "after_commit":
            raise PowerLoss()

    monkeypatch.setattr(service, "_checkpoint", crash)
    task = create(flow, root)
    with pytest.raises(PowerLoss):
        service._run(task["id"])
    restored = TransferService(db, threading.RLock(), auto_start=False)
    restored.recover()
    result = restored.get_workflow_task("op-1")
    assert result["status"] == "completed", result["message"]
    assert result["receipt"]["target_path"] == str(target / FILM)
    assert files_of(root) == files_of(target / FILM)
    restored.close()


def test_batch_setting_is_not_required_and_not_changed(flow):
    service, out, _, db = flow
    root = package(out)
    assert run(flow, create(flow, root))["status"] == "completed"
    assert db.execute("SELECT value FROM settings WHERE key='last_local_dir'").fetchone() is None


# ---------------------------------------------------------------------------------------------------------------------
# routes + endpoint file
# ---------------------------------------------------------------------------------------------------------------------
def test_routes_require_the_session_token(client, tmp_path):
    bare = client.__class__(client.app)
    assert bare.get("/api/workflow/archive/op-x").status_code == 403
    assert client.get("/api/workflow/archive/op-x").status_code == 404
    response = client.get("/api/workflow/archive/target", params={"performer_id": "nobody", "film_dir": FILM})
    assert response.status_code == 404


def test_endpoint_file_is_written_while_serving_and_removed_after(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from filmpaw_server.app import create_app

    db_path = tmp_path / "lib" / "library.db"
    db_path.parent.mkdir()
    endpoint = db_path.parent / "workflow-endpoint.json"
    with TestClient(create_app(db_path=db_path, port=43210)):
        data = json.loads(endpoint.read_text(encoding="utf-8"))
        assert data == {
            "port": 43210,
            "pid": os.getpid(),
            "token": SESSION_TOKEN,
            "db_path": str(db_path.resolve()),
        }
    assert not endpoint.exists()


def test_routes_archive_end_to_end(client, tmp_path):
    import time

    db = client.app.state.db
    out = tmp_path / "attempt" / "out"
    target = tmp_path / "nas" / "Actor"
    out.mkdir(parents=True)
    target.mkdir(parents=True)
    db.execute("INSERT INTO sources(id,unc_path,label) VALUES (1,?,'NAS')", (str(target.parent),))
    db.execute(
        "INSERT INTO performers(id,name,name_norm,source_id,folder_name,unc_path,first_seen_at,last_seen_at)"
        " VALUES ('actor','Actor','actor',1,'Actor',?,'now','now')",
        (str(target),),
    )
    db.commit()
    root = package(out)
    proof = client.get("/api/workflow/archive/target", params={"performer_id": "actor", "film_dir": FILM}).json()
    assert proof["absent"] is True
    body = {
        "operation_id": "op-route",
        "package_dir": str(root),
        "performer_id": "actor",
        "expected_target": proof["expected_target"],
        "files": files_of(root),
    }
    assert client.post("/api/workflow/archive", json=body).status_code == 202
    deadline = time.monotonic() + 30
    while (task := client.get("/api/workflow/archive/op-route").json())["status"] in ("queued", "running"):
        assert time.monotonic() < deadline
        time.sleep(0.05)
    assert task["status"] == "completed", task["message"]
    assert task["receipt"]["target_path"] == str(target / FILM)
    assert client.post("/api/workflow/archive", json=body).json()["id"] == task["id"]


def test_missing_performer_folder_and_missing_package_are_not_busy(flow, db):
    """412: confirm again; 409/503 stay 'try later' (IR01)."""
    service, out, target, _ = flow
    root = package(out)
    expected = expected_target(service, target)
    db.execute("UPDATE performers SET is_missing=1 WHERE id='actor'")
    db.commit()
    with pytest.raises(TransferError) as e:
        create(flow, root, expected=expected)
    assert e.value.status_code == 412
    db.execute("UPDATE performers SET is_missing=0 WHERE id='actor'")
    db.commit()
    gone = out / "OTHER-1"
    with pytest.raises(TransferError) as e:
        create(flow, gone, files=files_of(root), operation_id="op-gone", expected=expected)
    assert e.value.status_code == 412


def test_a_deleted_performer_folder_is_stale_not_an_outage(flow):
    """Codex F02 r1: a gone target must end the archive (412), never 'try later' forever."""
    service, out, target, _ = flow
    root = package(out)
    expected = expected_target(service, target)
    target.rmdir()
    with pytest.raises(TransferError) as proof:
        service.workflow_target("actor", FILM)
    assert proof.value.status_code == 412
    with pytest.raises(TransferError) as submit:
        create(flow, root, expected=expected)
    assert submit.value.status_code == 412
