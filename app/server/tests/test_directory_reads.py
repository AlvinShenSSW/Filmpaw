"""Bounded read-only retries distinguish transient SMB errors from refusals."""

import errno
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from filmpaw_server.directory_reads import directory_error_retryable
from filmpaw_server.scan import SourceUnreachable
from filmpaw_server.transfer import TransferError
from tests.test_archive_api import seed_pair


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
