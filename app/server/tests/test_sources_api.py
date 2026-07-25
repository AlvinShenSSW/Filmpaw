from pathlib import Path

from tests.conftest import add_performer_folder


def test_add_list_delete_source(client, source_dir: Path) -> None:
    add_performer_folder(source_dir, "倉木華")
    resp = client.post("/api/sources", json={"unc_path": str(source_dir)})
    assert resp.status_code == 201
    sid = resp.json()["id"]

    # duplicate -> 409 (path normalized with trailing backslash)
    assert client.post("/api/sources", json={"unc_path": str(source_dir)}).status_code == 409

    listed = client.get("/api/sources").json()
    assert len(listed) == 1
    row = listed[0]
    assert row["performer_count"] == 0 and row["reachable"] is True
    assert row["last_scan_at"] is None
    assert row["label"]  # defaulted from path tail

    assert client.delete(f"/api/sources/{sid}").status_code == 204
    assert client.get("/api/sources").json() == []
    assert client.delete(f"/api/sources/{sid}").status_code == 404


def test_add_unreachable_source_422(client) -> None:
    resp = client.post("/api/sources", json={"unc_path": "Z:\\definitely\\missing\\"})
    assert resp.status_code == 422


def test_scan_endpoint_and_counts(client, source_dir: Path) -> None:
    add_performer_folder(source_dir, "倉木華", with_jpg=True)
    add_performer_folder(source_dir, "小红")
    sid = client.post("/api/sources", json={"unc_path": str(source_dir)}).json()["id"]

    r = client.post(f"/api/sources/{sid}/scan")
    assert r.status_code == 200
    assert r.json() == {"added": 2, "refreshed": 0, "missing": 0}

    listed = client.get("/api/sources").json()[0]
    assert listed["performer_count"] == 2
    assert listed["last_scan_at"] is not None

    assert client.post("/api/sources/9999/scan").status_code == 404


def test_scan_unreachable_503_records_untouched(client, source_dir: Path, tmp_path: Path) -> None:
    add_performer_folder(source_dir, "A子")
    sid = client.post("/api/sources", json={"unc_path": str(source_dir)}).json()["id"]
    client.post(f"/api/sources/{sid}/scan")

    import shutil

    shutil.rmtree(source_dir)
    r = client.post(f"/api/sources/{sid}/scan")
    assert r.status_code == 503
    # counts unchanged (A子 not marked missing by an unreachable-source scan)
    assert client.get("/api/sources").json()[0]["performer_count"] == 1


def test_scan_all_isolates_failures(client, source_dir: Path, tmp_path: Path) -> None:
    add_performer_folder(source_dir, "B子")
    ok_id = client.post("/api/sources", json={"unc_path": str(source_dir)}).json()["id"]

    dead = tmp_path / "dead_src"
    dead.mkdir()
    dead_id = client.post("/api/sources", json={"unc_path": str(dead)}).json()["id"]
    dead.rmdir()  # now unreachable

    results = {r["source_id"]: r for r in client.post("/api/scan-all").json()}
    assert results[ok_id]["ok"] is True and results[ok_id]["added"] == 1
    assert results[dead_id]["ok"] is False


def test_delete_source_cascades_performers(client, source_dir: Path) -> None:
    add_performer_folder(source_dir, "C子")
    sid = client.post("/api/sources", json={"unc_path": str(source_dir)}).json()["id"]
    client.post(f"/api/sources/{sid}/scan")
    assert client.get("/api/sources").json()[0]["performer_count"] == 1
    client.delete(f"/api/sources/{sid}")
    assert client.get("/api/sources").json() == []


def test_concurrent_scans_do_not_race(client, source_dir: Path) -> None:
    """Codex P1 regression: overlapping scan requests must serialize on the
    db lock instead of racing the shared connection into a 500."""
    from concurrent.futures import ThreadPoolExecutor

    for i in range(8):
        add_performer_folder(source_dir, f"P{i}")
    sid = client.post("/api/sources", json={"unc_path": str(source_dir)}).json()["id"]

    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(client.post, f"/api/sources/{sid}/scan") for _ in range(4)]
        futures += [pool.submit(client.post, "/api/scan-all") for _ in range(2)]
        codes = [f.result().status_code for f in futures]
    assert all(c == 200 for c in codes), codes
