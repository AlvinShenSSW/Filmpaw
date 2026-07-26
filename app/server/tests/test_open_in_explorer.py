"""#28: explorer launch must not truncate paths, and must stay fire-and-forget.

The path-integrity tests assert only that the FULL path reaches the OS launch
call byte-for-byte (the truncation bug was a lost suffix). They deliberately do
not claim anything about how explorer renders each character — that is only
verifiable by hand.
"""

import sys

import pytest

import filmpaw_server.routes_performers as rp

SPECIAL_NAMES = [
    "沙月恵奈,月野かすみ",  # the reported bug: comma
    "with space",
    "amp&and",
    "hash#tag",
    "pct%20name",
    "倉木華",
    "明里つむぎ",
]


@pytest.mark.skipif(sys.platform != "win32", reason="Windows launch path")
@pytest.mark.parametrize("name", SPECIAL_NAMES)
def test_full_path_reaches_the_os_call_untruncated(monkeypatch, tmp_path, name) -> None:
    """Regression for the comma truncation: os.startfile must receive the whole
    path as a single value (no command-line parsing in between)."""
    seen: list[str] = []
    monkeypatch.setattr(rp.os, "startfile", lambda p: seen.append(p), raising=False)

    target = tmp_path / name
    target.mkdir()
    rp.open_in_explorer(str(target))

    assert seen == [str(target)]  # byte-for-byte, suffix intact
    assert seen[0].endswith(name)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows launch path")
def test_unc_and_drive_root_pass_through_unchanged(monkeypatch) -> None:
    seen: list[str] = []
    monkeypatch.setattr(rp.os, "startfile", lambda p: seen.append(p), raising=False)
    paths = [r"\\Eagle\Video Station\女优III\沙月恵奈,月野かすみ", "C:\\"]
    for p in paths:
        rp.open_in_explorer(p)
    assert seen == paths


def test_unreachable_path_does_not_raise_real_impl(tmp_path) -> None:
    """GLM P1: os.startfile raises OSError where Popen did not — an offline NAS
    between scans must not turn a 204 launch into a 500. Runs the REAL
    implementation (no monkeypatch of open_in_explorer)."""
    gone = tmp_path / "definitely-not-here" / "nor-this"
    rp.open_in_explorer(str(gone))  # must not raise


def test_open_pair_stays_204_when_nas_path_is_unreachable(
    client, source_dir, tmp_path, monkeypatch
) -> None:
    """The pair contract is launch-and-forget: an unreachable NAS path (offline
    between scans, so is_missing is still 0) must still return 204."""
    from tests.conftest import add_performer_folder

    add_performer_folder(source_dir, "离线测试")
    sid = client.post("/api/sources", json={"unc_path": str(source_dir)}).json()["id"]
    client.post(f"/api/sources/{sid}/scan")
    pid = client.get("/api/performers").json()["items"][0]["id"]

    local_root = tmp_path / "downloads"
    (local_root / "离线测试").mkdir(parents=True)
    client.put("/api/settings", json={"last_local_dir": str(local_root)})

    # real launcher, but the OS call fails as it would for an offline share
    def boom(_p):
        raise OSError(2, "The network path was not found")

    monkeypatch.setattr(rp.os, "startfile", boom, raising=False)
    monkeypatch.setattr(rp.subprocess, "Popen", lambda *a, **k: (_ for _ in ()).throw(OSError()))

    r = client.post(
        "/api/open-pair",
        json={"subdir": "离线测试", "performer_id": pid},
    )
    assert r.status_code == 204
