"""NAS capability failures injected around real, disposable Windows handles."""

import ctypes
import sys

import pytest

from filmpaw_server import cleanup, path_resources
from filmpaw_server.transfer import TransferError
from tests.test_transfer_concurrent import multi  # noqa: F401


pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows native handles")


class NativeCall:
    def __init__(self, call):
        self.call = call

    def __call__(self, *args):
        return self.call(*args)


def inject_file_id_failure(monkeypatch, code=87):
    original = ctypes.WinDLL
    calls = []

    def library(*args, **kwargs):
        kernel = original(*args, **kwargs)
        original_ex = kernel.GetFileInformationByHandleEx
        original_ex.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_ulong]

        def get_info(handle, kind, buffer, length):
            calls.append(("ex", handle, kind))
            if kind == 18:
                ctypes.set_last_error(code)
                return 0
            return original_ex(handle, kind, buffer, length)

        kernel.GetFileInformationByHandleEx = NativeCall(get_info)
        return kernel

    monkeypatch.setattr(ctypes, "WinDLL", library)
    return calls


@pytest.mark.parametrize("code", [1, 50, 87])
def test_capture_supports_ntfs_without_file_id_info(tmp_path, monkeypatch, code):
    actor = tmp_path / "actor"
    actor.mkdir()
    native = path_resources.capture(actor)
    inject_file_id_failure(monkeypatch, code)

    result = path_resources.capture(actor)

    assert result["identity"] == native["identity_aliases"][0]
    assert result["identity"][0] == "legacy64"
    assert result["identity"][2] == actor.stat().st_ino
    assert result["ancestors"]
    assert actor.samefile(result["path"])


def test_cleanup_exact_handle_supports_ntfs_without_file_id_info(tmp_path, monkeypatch):
    actor = tmp_path / "empty"
    actor.mkdir()
    inject_file_id_failure(monkeypatch)

    with cleanup._native_directory(tmp_path) as handle:
        root = cleanup._native_info(handle)
    candidate = cleanup._snapshot(root)[0]
    result = cleanup._remove_empty(root, candidate, lambda: None)

    assert result[0] == "deleted"
    assert not actor.exists()


class FakeKernel:
    """Model metadata only; every API records the exact handle it receives."""
    def __init__(self, *, error=87, filesystem="NTFS", index=987, volume=123,
                 attributes=0x10, legacy_attributes=0x10, attribute_error=None,
                 legacy_error=None, volume_error=None, native_id=b"\x12" * 16):
        self.calls = []

        def ex(handle, kind, pointer, length):
            self.calls.append((kind, handle))
            if kind == 9:
                if attribute_error is not None:
                    ctypes.set_last_error(attribute_error)
                    return 0
                pointer._obj.attributes = attributes
            elif error is not None:
                ctypes.set_last_error(error)
                return 0
            else:
                pointer._obj.volume = (1 << 40) + 321
                pointer._obj.file_id[:] = native_id
            return 1

        def legacy(handle, pointer):
            self.calls.append(("legacy", handle))
            if legacy_error is not None:
                ctypes.set_last_error(legacy_error)
                return 0
            pointer._obj.attributes = legacy_attributes
            pointer._obj.volume = volume
            pointer._obj.index_high = index >> 32
            pointer._obj.index_low = index & 0xFFFFFFFF
            return 1

        def filesystem_info(handle, label, size, serial, component, flags, name, capacity):
            self.calls.append(("filesystem", handle))
            if volume_error is not None:
                ctypes.set_last_error(volume_error)
                return 0
            name.value = filesystem
            return 1

        self.GetFileInformationByHandleEx = NativeCall(ex)
        self.GetFileInformationByHandle = NativeCall(legacy)
        self.GetVolumeInformationByHandleW = NativeCall(filesystem_info)


@pytest.mark.parametrize("error", [1, 50, 87])
def test_legacy_identity_uses_same_handle_and_separate_namespace(error):
    from filmpaw_server.windows_identity import directory_identity
    kernel = FakeKernel(error=error, index=(1 << 45) + 31, volume=0)
    handle = 0x123456789ABC

    result = directory_identity(handle, kernel=kernel)

    assert result["identity"] == ["legacy64", 0, (1 << 45) + 31]
    assert [name for name, _ in kernel.calls] == [9, 18, "filesystem", "legacy"]
    assert all(actual == handle for _, actual in kernel.calls)


@pytest.mark.parametrize("error", [5, 32, 53, 64, 121])
def test_permission_network_and_sharing_errors_never_downgrade(error):
    from filmpaw_server.windows_identity import directory_identity
    kernel = FakeKernel(error=error)

    with pytest.raises(OSError) as caught:
        directory_identity(123, kernel=kernel)

    assert caught.value.winerror == error
    assert [name for name, _ in kernel.calls] == [9, 18]


@pytest.mark.parametrize("configuration", [
    {"filesystem": "ReFS"}, {"filesystem": "exFAT"}, {"filesystem": ""},
    {"index": 0}, {"index": 0xFFFFFFFFFFFFFFFF},
    {"attributes": 0}, {"attributes": 0x410},
    {"legacy_attributes": 0}, {"legacy_attributes": 0x410},
    {"attribute_error": 87}, {"legacy_error": 5}, {"volume_error": 53},
])
def test_unreliable_fallback_identity_is_rejected(configuration):
    from filmpaw_server.windows_identity import directory_identity
    with pytest.raises(OSError):
        directory_identity(123, kernel=FakeKernel(**configuration))


def test_native_identity_is_unchanged_and_alias_is_not_truncated():
    from filmpaw_server.windows_identity import directory_identity
    kernel = FakeKernel(error=None)

    result = directory_identity(123, kernel=kernel, include_alias=True)

    assert result["identity"] == [(1 << 40) + 321, "12" * 16]
    assert result["identity_aliases"] == [["legacy64", 123, 987]]


@pytest.mark.parametrize("configuration", [
    {"legacy_error": 5}, {"volume_error": 50}, {"filesystem": "ReFS"}, {"index": 0},
])
def test_optional_bridge_failure_does_not_break_native_browsing(configuration):
    from filmpaw_server.windows_identity import directory_identity
    result = directory_identity(123, kernel=FakeKernel(error=None, **configuration), include_alias=True)
    assert result["identity"] == [(1 << 40) + 321, "12" * 16]
    assert result["identity_aliases"] == []


@pytest.mark.parametrize("identifier", [bytes(16), b"\xff" * 16])
def test_invalid_native_identity_does_not_fall_back(identifier):
    from filmpaw_server.windows_identity import directory_identity
    kernel = FakeKernel(error=None, native_id=identifier)
    with pytest.raises(OSError):
        directory_identity(123, kernel=kernel, include_alias=True)
    assert [name for name, _ in kernel.calls] == [9, 18]


def test_mixed_modes_bridge_root_and_ancestors_without_guessing_paths(tmp_path, monkeypatch):
    parent, child, sibling = tmp_path / "parent", tmp_path / "parent" / "child", tmp_path / "other"
    child.mkdir(parents=True)
    sibling.mkdir()
    native_parent, native_child = path_resources.capture(parent), path_resources.capture(child)
    native_sibling = path_resources.capture(sibling)
    inject_file_id_failure(monkeypatch)
    legacy_parent, legacy_child = path_resources.capture(parent), path_resources.capture(child)
    legacy_sibling = path_resources.capture(sibling)
    # Distinct spellings prevent the path-string fallback from hiding missing ID bridges.
    for resource in [legacy_parent, legacy_child, legacy_sibling]:
        resource["path"] = "Z:\\different-server-alias\\" + str(resource["identity"][2])
    for first, second in [(native_parent, legacy_parent), (native_child, legacy_child),
                          (native_parent, legacy_child), (native_child, legacy_parent)]:
        assert path_resources.overlaps(first, second)
        assert path_resources.overlaps(second, first)
    assert not path_resources.overlaps(native_sibling, legacy_child)
    assert not path_resources.overlaps(legacy_sibling, native_child)
    native_child["identity_aliases"] = []
    assert path_resources.overlaps(native_child, legacy_sibling)  # unknown is not independent


def test_native_context_then_legacy_context_owns_same_actor_and_lists_movies(multi, monkeypatch):
    service, batch, nas, _ = multi
    native = service.create_context(str(batch), "Actor-A", "Actor-A")
    service.submit(native["id"], ["MOV-001"], "native-task")
    inject_file_id_failure(monkeypatch)

    legacy = service.create_context(str(batch), "Actor-A", "Actor-A")
    assert legacy["source_error"] is None and legacy["target_error"] is None
    assert [item["name"] for item in legacy["source_items"]] == ["MOV-001"]
    with pytest.raises(TransferError, match="占用|任务"):
        service.submit(legacy["id"], ["MOV-001"], "duplicate-task")
    other = service.create_context(str(batch), "Actor-B", "Actor-B")
    assert other["source_error"] is None and other["target_error"] is None
    task = service.submit(other["id"], ["MOV-001"], "legacy-task")
    service._run(task["id"])
    assert service.get_task(task["id"])["status"] == "completed"
    assert (nas / "Actor-B" / "MOV-001" / "video.mp4").exists()
    assert not (batch / "Actor-B" / "MOV-001").exists()


def test_cleanup_mode_change_preserves_candidate(tmp_path, monkeypatch):
    actor = tmp_path / "empty"
    actor.mkdir()
    with cleanup._native_directory(tmp_path) as handle:
        root = cleanup._native_info(handle)
    candidate = cleanup._snapshot(root)[0]
    inject_file_id_failure(monkeypatch)

    with pytest.raises(TransferError, match="身份"):
        cleanup._remove_empty(root, candidate, lambda: None)
    assert actor.is_dir()


@pytest.mark.parametrize("fail", [False, True])
def test_capture_always_closes_its_native_handle(tmp_path, monkeypatch, fail):
    from ctypes import wintypes
    original = path_resources.directory_identity
    seen = []

    def identity(handle, **kwargs):
        seen.append(handle)
        if fail:
            raise ctypes.WinError(53)
        return original(handle, **kwargs)

    monkeypatch.setattr(path_resources, "directory_identity", identity)
    if fail:
        with pytest.raises(OSError):
            path_resources._info(tmp_path)
    else:
        path_resources._info(tmp_path)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    query = kernel.GetHandleInformation
    query.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    query.restype = wintypes.BOOL
    flags = wintypes.DWORD()
    assert query(seen[0], ctypes.byref(flags)) == 0
    assert ctypes.get_last_error() == 6  # ERROR_INVALID_HANDLE


@pytest.mark.parametrize("consumer", ["capture", "cleanup"])
def test_final_path_failure_is_not_downgraded(tmp_path, monkeypatch, consumer):
    original = ctypes.WinDLL
    flags_seen = []

    def library(*args, **kwargs):
        kernel = original(*args, **kwargs)
        def final(handle, buffer, size, flags):
            flags_seen.append(flags)
            ctypes.set_last_error(87)
            return 0
        kernel.GetFinalPathNameByHandleW = NativeCall(final)
        return kernel

    monkeypatch.setattr(ctypes, "WinDLL", library)
    with pytest.raises(OSError) as caught:
        if consumer == "capture":
            path_resources._info(tmp_path)
        else:
            with cleanup._native_directory(tmp_path) as handle:
                cleanup._native_info(handle)
    assert caught.value.winerror == 87
    assert flags_seen == [0]


def test_fallback_cleanup_keeps_ads_and_nonempty_directories(tmp_path, monkeypatch):
    empty, full, ads = [tmp_path / name for name in ["empty", "full", "ads"]]
    for path in [empty, full, ads]:
        path.mkdir()
    (full / "hidden.nfo").write_bytes(b"metadata")
    stream = type(ads)(str(ads) + ":metadata")
    stream.write_bytes(b"additional data")
    inject_file_id_failure(monkeypatch)
    with cleanup._native_directory(tmp_path) as handle:
        root = cleanup._native_info(handle)

    results = {item["name"]: cleanup._remove_empty(root, item, lambda: None)[0]
               for item in cleanup._snapshot(root)}

    assert results == {"empty": "deleted", "full": "not_empty", "ads": "not_empty"}
    assert stream.read_bytes() == b"additional data"
    assert (full / "hidden.nfo").read_bytes() == b"metadata"


def test_native_success_with_reparse_alias_is_rejected():
    from filmpaw_server.windows_identity import NotPlainDirectory, directory_identity
    with pytest.raises(NotPlainDirectory):
        directory_identity(123, kernel=FakeKernel(error=None, legacy_attributes=0x410),
                           include_alias=True)
