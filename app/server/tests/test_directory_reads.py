"""Bounded read-only retries distinguish transient SMB errors from refusals."""

import errno
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from filmpaw_server.directory_reads import directory_error_retryable
from filmpaw_server.scan import SourceUnreachable
from filmpaw_server.transfer import TransferError
from tests.test_archive_api import seed_pair


def _fail_entry_stat(monkeypatch, root, name, error):
    """Exercise the Windows hidden-attribute check on every test platform."""
    import filmpaw_server.scan as scan

    attempts = []
    original_scandir = os.scandir

    class Entry:
        def __init__(self, entry):
            self.entry = entry
            self.name = entry.name

        def is_dir(self, *, follow_symlinks):
            return self.entry.is_dir(follow_symlinks=follow_symlinks)

        def stat(self, *, follow_symlinks):
            if self.name == name:
                attempts.append(self.name)
                raise error
            result = self.entry.stat(follow_symlinks=follow_symlinks)
            return SimpleNamespace(st_file_attributes=getattr(result, "st_file_attributes", 0))

    @contextmanager
    def scandir(path):
        assert str(path) == str(root)
        with original_scandir(path) as entries:
            yield (Entry(entry) for entry in entries)

    # Replace this module's facades, not the shared os/sys modules used by pytest.
    monkeypatch.setattr(scan, "os", SimpleNamespace(stat=os.stat, scandir=scandir))
    monkeypatch.setattr(scan, "sys", SimpleNamespace(platform="win32"))
    return attempts


@pytest.mark.parametrize("code", [32, 33, 53, 64, 67, 121, 170, 1231, 1460])
def test_windows_transient_code_takes_precedence_over_errno(code):
    error = OSError(errno.ENOENT, "Windows network failure")
    error.winerror = code
    assert directory_error_retryable(error)


@pytest.mark.parametrize("error", [
    FileNotFoundError(errno.ENOENT, "missing"), PermissionError(errno.EACCES, "denied"),
    NotADirectoryError(errno.ENOTDIR, "not a directory"),
    TransferError("directory identity changed", 409), TransferError("reparse point refused"),
])
def test_permanent_paths_permissions_and_safety_refusals_are_not_retryable(error):
    assert not directory_error_retryable(error)


@pytest.mark.parametrize("error,retryable", [
    (TimeoutError(errno.ETIMEDOUT, "NAS still waking"), True),
    (ConnectionResetError(errno.ECONNRESET, "SMB disconnected"), True),
    (PermissionError(errno.EACCES, "not allowed"), False),
    (FileNotFoundError(errno.ENOENT, "actor removed"), False),
    (TransferError("directory identity changed", 409), False),
])
def test_context_exposes_independent_retry_hints(client, tmp_path, monkeypatch, error, retryable):
    import filmpaw_server.transfer as transfer
    batch, _, target, pid = seed_pair(client, tmp_path)
    original = transfer._directory
    def fail_target(path, expected=None):
        if path == target:
            raise error
        return original(path, expected)
    monkeypatch.setattr(transfer, "_directory", fail_target)
    response = client.post("/api/archive/contexts", json={
        "local_dir": str(batch), "subdir": "Actor", "performer_id": pid,
    })
    assert response.status_code == 201
    context = response.json()
    assert context["target_error"] and context["target_retryable"] is retryable
    assert context["source_error"] is None and context["source_retryable"] is False
    assert len(context["source_items"]) == 1
    refused = client.post("/api/archive/transfers", json={
        "context_id": context["id"], "names": ["Film, 10"], "request_id": "unreadable",
    })
    assert refused.status_code == 409
    assert client.get("/api/archive/transfers/latest").json() is None


def test_transient_second_entry_stat_propagates_to_side_and_later_read_recovers(client, tmp_path, monkeypatch):
    import filmpaw_server.transfer as transfer
    batch, source, _, pid = seed_pair(client, tmp_path)
    original = transfer._plain_stat
    calls = 0
    def flaky(path):
        nonlocal calls
        if path == source / "Film, 10":
            calls += 1
            if calls == 1:
                raise TimeoutError(errno.ETIMEDOUT, "temporary entry stat timeout")
        return original(path)
    monkeypatch.setattr(transfer, "_plain_stat", flaky)
    body = {"local_dir": str(batch), "subdir": "Actor", "performer_id": pid}
    failed = client.post("/api/archive/contexts", json=body).json()
    assert failed["source_retryable"] and failed["source_error"]
    assert failed["source_items"] == [] and not failed["target_retryable"]
    assert calls == 1  # the backend itself did not blindly replay a filesystem read
    recovered = client.post("/api/archive/contexts", json=body).json()
    assert not recovered["source_retryable"] and recovered["source_error"] is None
    assert len(recovered["source_items"]) == 1


def test_context_gate_bounds_hanging_reads_without_holding_database_or_exit(client, tmp_path, monkeypatch):
    batch, _, target, pid = seed_pair(client, tmp_path)
    service = client.app.state.transfers
    original = service._list
    entered, release = threading.Event(), threading.Event()
    count = 0
    def blocked(path):
        nonlocal count
        if path == target:
            count += 1
            entered.set()
            assert release.wait(5), "test release timed out"
        return original(path)
    monkeypatch.setattr(service, "_list", blocked)
    body = {"local_dir": str(batch), "subdir": "Actor", "performer_id": pid}
    with ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(client.post, "/api/archive/contexts", json=body)
        try:
            assert entered.wait(3)
            for _ in range(4):
                assert client.post("/api/archive/contexts", json=body).status_code == 503
            assert count == 1
            assert service.db_lock.acquire(timeout=0.1)
            service.db_lock.release()
            assert client.get("/api/health").status_code == 200
        finally:
            release.set()
        assert first.result(3).status_code == 201
    assert client.post("/api/archive/contexts", json=body).status_code == 201
    assert count == 2


def test_read_gate_does_not_block_safe_exit_when_no_transfer_exists(client, tmp_path, monkeypatch):
    batch, _, target, pid = seed_pair(client, tmp_path)
    service = client.app.state.transfers
    original = service._list
    entered, release = threading.Event(), threading.Event()
    def blocked(path):
        if path == target:
            entered.set()
            assert release.wait(5)
        return original(path)
    monkeypatch.setattr(service, "_list", blocked)
    body = {"local_dir": str(batch), "subdir": "Actor", "performer_id": pid}
    with ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(client.post, "/api/archive/contexts", json=body)
        try:
            assert entered.wait(3)
            assert client.post("/api/archive/prepare-exit").json()["safe"] is True
        finally:
            release.set()
        assert first.result(3).status_code == 409  # closing invalidates the late context


@pytest.mark.parametrize("error,status", [
    (TimeoutError(errno.ETIMEDOUT, "NAS slow"), 503),
    (FileNotFoundError(errno.ENOENT, "deleted"), 422),
    (PermissionError(errno.EACCES, "denied"), 422),
])
def test_local_subdirs_preserves_classification_and_releases_gate(client, tmp_path, monkeypatch, error, status):
    import filmpaw_server.routes_performers as routes
    original = routes.list_subdirs
    def unavailable(path):
        raise SourceUnreachable(path) from error
    monkeypatch.setattr(routes, "list_subdirs", unavailable)
    assert client.get("/api/local/subdirs", params={"path": str(tmp_path)}).status_code == status
    monkeypatch.setattr(routes, "list_subdirs", original)
    assert client.get("/api/local/subdirs", params={"path": str(tmp_path)}).status_code == 200


@pytest.mark.parametrize("error,status", [
    (TimeoutError(errno.ETIMEDOUT, "entry stat timed out"), 503),
    (FileNotFoundError(errno.ENOENT, "entry disappeared"), 422),
    (PermissionError(errno.EACCES, "entry access denied"), 422),
])
def test_local_subdirs_entry_stat_failure_rejects_partial_list_and_recovers(
    client, tmp_path, monkeypatch, error, status,
):
    root = tmp_path / "batch"
    root.mkdir()
    for name in ["A visible", "B unreadable"]:
        (root / name).mkdir()
    with monkeypatch.context() as patch:
        attempts = _fail_entry_stat(patch, root, "B unreadable", error)
        failed = client.get("/api/local/subdirs", params={"path": str(root)})
        assert failed.status_code == status
        assert "subdirs" not in failed.json()  # no successful but incomplete snapshot
        assert attempts == ["B unreadable"]  # retries belong to the caller

    recovered = client.get("/api/local/subdirs", params={"path": str(root)})
    assert recovered.status_code == 200
    assert recovered.json()["subdirs"] == ["A visible", "B unreadable"]


def test_scan_entry_stat_failure_preserves_all_records_and_scan_timestamp(db, tmp_path, monkeypatch):
    from filmpaw_server.scan import scan_source

    root = tmp_path / "nas"
    root.mkdir()
    for name in ["A existing", "B unreadable"]:
        (root / name).mkdir()
    source_id = db.execute(
        "INSERT INTO sources(unc_path, label) VALUES (?, ?)", (str(root), "NAS"),
    ).lastrowid
    db.commit()
    scan_source(db, source_id)
    # A deterministic old timestamp makes an unintended successful rescan visible.
    db.execute("UPDATE sources SET last_scan_at='before failure' WHERE id=?", (source_id,))
    db.commit()
    before = [dict(row) for row in db.execute("SELECT * FROM performers ORDER BY folder_name")]
    source_before = dict(db.execute("SELECT * FROM sources WHERE id=?", (source_id,)).fetchone())
    (root / "C new").mkdir()
    error = TimeoutError(errno.ETIMEDOUT, "entry stat timed out")
    with monkeypatch.context() as patch:
        attempts = _fail_entry_stat(patch, root, "B unreadable", error)
        with pytest.raises(SourceUnreachable) as failure:
            scan_source(db, source_id)
        assert failure.value.__cause__ is error
        assert attempts == ["B unreadable"]

    assert not db.in_transaction
    db.commit()  # an unrelated later commit must not persist partial scan writes
    after = [dict(row) for row in db.execute("SELECT * FROM performers ORDER BY folder_name")]
    assert after == before  # no new row, refreshed row or false missing actor
    assert dict(db.execute("SELECT * FROM sources WHERE id=?", (source_id,)).fetchone()) == source_before
    recovered = scan_source(db, source_id)
    assert (recovered.added, recovered.refreshed, recovered.missing) == (1, 2, 0)


def test_local_subdirs_gate_bounds_concurrent_os_reads(client, tmp_path, monkeypatch):
    import filmpaw_server.routes_performers as routes
    original = routes.list_subdirs
    entered, release = threading.Event(), threading.Event()
    count = 0
    def blocked(path):
        nonlocal count
        count += 1
        entered.set()
        assert release.wait(5)
        return original(path)
    monkeypatch.setattr(routes, "list_subdirs", blocked)
    with ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(client.get, "/api/local/subdirs", params={"path": str(tmp_path)})
        try:
            assert entered.wait(3)
            assert client.get("/api/local/subdirs", params={"path": str(tmp_path)}).status_code == 503
            assert count == 1
        finally:
            release.set()
        assert first.result(3).status_code == 200
    assert client.get("/api/local/subdirs", params={"path": str(tmp_path)}).status_code == 200
    assert count == 2
