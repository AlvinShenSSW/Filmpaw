import io
from uuid import uuid4

import pytest
from PIL import Image

from filmpaw_server.scan_worker import collect_snapshot, worker_main, CollectionError
from filmpaw_server.workflow_models import ScanProfile, ScanRequest


def request(path, **changes):
    return ScanRequest(scan_id=str(uuid4()), attempt_id=str(uuid4()), root_id=str(uuid4()),
                       host_id=str(uuid4()), export_id=str(uuid4()), root_revision=1,
                       mapping_revision=1, owner_generation=1, kind="scan_index",
                       private_root_path=str(path), **changes)


def test_collect_has_complete_relative_observations_and_explicit_actor_id(tmp_path):
    actor = tmp_path / "Actor A"
    film = actor / "Film 001"
    film.mkdir(parents=True)
    Image.new("RGB", (640, 960), "red").save(actor / "folder.jpg")
    (film / "movie.nfo").write_text(
        '<movie><title>Synthetic</title><num>CODE-001</num>'
        '<actor><name>Actor A</name><uniqueid type="catalog">123</uniqueid></actor>'
        '<actor><name>Co-star</name><uniqueid type="catalog">999</uniqueid></actor></movie>',
        encoding="utf-8")
    snapshot = collect_snapshot(request(tmp_path))
    assert snapshot.coverage == "complete"
    assert snapshot.actors[0].thumbnail.state == "replace"
    assert snapshot.films[0].code == "CODE-001"
    assert snapshot.films[0].relative_film_path == "Actor A/Film 001"
    assert [e.value for e in snapshot.actors[0].evidence if e.kind == "explicit_external_id"] == ["123"]


def test_unsafe_or_oversized_nfo_is_unknown_not_expanded(tmp_path):
    film = tmp_path / "Actor" / "Film"
    film.mkdir(parents=True)
    (film / "movie.nfo").write_text('<!DOCTYPE movie [<!ENTITY e "oops">]><movie><title>&e;</title></movie>')
    snapshot = collect_snapshot(request(tmp_path))
    assert snapshot.coverage == "complete"
    assert "metadata_unknown" in snapshot.optional_errors
    assert snapshot.films[0].title is None
    (film / "movie.nfo").write_bytes(b"x" * 1100)
    snapshot = collect_snapshot(request(tmp_path, profile=ScanProfile(nfo_bytes=1024)))
    assert "metadata_unknown" in snapshot.optional_errors


def test_actor_limit_is_incomplete_not_silent_truncation(tmp_path):
    (tmp_path / "A").mkdir()
    (tmp_path / "B").mkdir()
    with pytest.raises(CollectionError, match="partial"):
        collect_snapshot(request(tmp_path, profile=ScanProfile(max_actors=1)))


def test_worker_without_go_does_not_call_collector(monkeypatch):
    from filmpaw_server import scan_worker
    monkeypatch.setattr(scan_worker, "collect_snapshot", lambda *_: pytest.fail("no GO"))
    output = io.BytesIO()
    assert worker_main(io.BytesIO(b""), output) != 0


def test_missing_root_has_fixed_error_without_private_path(tmp_path):
    with pytest.raises(CollectionError) as error:
        collect_snapshot(request(tmp_path / "private-missing"))
    assert str(tmp_path) not in str(error.value)


def test_wide_encoded_entity_declarations_are_metadata_unknown(tmp_path):
    film = tmp_path / "Actor" / "Film"
    film.mkdir(parents=True)
    (film / "movie.nfo").write_bytes('<!DOCTYPE movie [<!ENTITY e "expanded">]><movie><title>&e;</title></movie>'.encode("utf-16"))
    snapshot = collect_snapshot(request(tmp_path))
    assert snapshot.films[0].title is None
    assert "metadata_unknown" in snapshot.optional_errors


def test_real_windows_sharing_error_is_transient_and_old_thumbnail_is_preserved(tmp_path):
    import ctypes
    import sys
    from ctypes import wintypes as w
    if sys.platform != "win32":
        pytest.skip("Windows sharing mode")
    actor = tmp_path / "Actor"
    actor.mkdir()
    image = actor / "folder.jpg"
    Image.new("RGB", (5, 5), "red").save(image)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.argtypes = [w.LPCWSTR, w.DWORD, w.DWORD, w.LPVOID, w.DWORD, w.DWORD, w.HANDLE]
    kernel.CreateFileW.restype = w.HANDLE
    kernel.CloseHandle.argtypes, kernel.CloseHandle.restype = [w.HANDLE], w.BOOL
    handle = kernel.CreateFileW(str(image), 0x80000000, 0, None, 3, 0, None)
    assert handle != ctypes.c_void_p(-1).value
    try:
        snapshot = collect_snapshot(request(tmp_path))
        assert snapshot.coverage == "complete"
        assert snapshot.actors[0].thumbnail.state == "unreadable"
    finally:
        assert kernel.CloseHandle(handle)
    assert collect_snapshot(request(tmp_path)).actors[0].thumbnail.state == "replace"
