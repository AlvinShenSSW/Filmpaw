import ctypes
import sqlite3
import sys
from pathlib import Path

import pytest
from PIL import Image

from filmpaw_server.scan import THUMB_MAX_SIDE, SourceUnreachable, list_subdirs, scan_source
from tests.conftest import add_performer_folder


def _add_source(db: sqlite3.Connection, root: Path) -> int:
    cur = db.execute("INSERT INTO sources(unc_path, label) VALUES (?, ?)", (str(root), "t"))
    db.commit()
    return cur.lastrowid


def _rows(db: sqlite3.Connection):
    return {
        r["folder_name"]: r
        for r in db.execute("SELECT * FROM performers").fetchall()
    }


def test_scan_adds_new_folders(db, source_dir) -> None:
    add_performer_folder(source_dir, "倉木華")
    add_performer_folder(source_dir, "小红")
    sid = _add_source(db, source_dir)
    r = scan_source(db, sid)
    assert (r.added, r.refreshed, r.missing) == (2, 0, 0)
    rows = _rows(db)
    assert rows["倉木華"]["name_norm"] == rows["倉木華"]["name_norm"]
    assert rows["倉木華"]["is_missing"] == 0
    assert rows["倉木華"]["unc_path"].endswith("倉木華")


def test_rescan_refreshes_and_marks_missing_without_delete(db, source_dir) -> None:
    add_performer_folder(source_dir, "A子")
    gone = add_performer_folder(source_dir, "B子")
    sid = _add_source(db, source_dir)
    scan_source(db, sid)
    import shutil

    shutil.rmtree(gone)
    r = scan_source(db, sid)
    assert (r.added, r.refreshed, r.missing) == (0, 1, 1)
    rows = _rows(db)
    assert rows["B子"]["is_missing"] == 1  # marked, not deleted
    assert rows["A子"]["is_missing"] == 0
    # already-missing folders are NOT re-counted on the next scan
    r2 = scan_source(db, sid)
    assert (r2.added, r2.refreshed, r2.missing) == (0, 1, 0)


def test_missing_folder_reappears_clears_flag(db, source_dir) -> None:
    d = add_performer_folder(source_dir, "C子")
    sid = _add_source(db, source_dir)
    scan_source(db, sid)
    import shutil

    shutil.rmtree(d)
    scan_source(db, sid)
    add_performer_folder(source_dir, "C子")
    r = scan_source(db, sid)
    assert r.refreshed == 1
    assert _rows(db)["C子"]["is_missing"] == 0


def test_unreachable_source_raises_and_touches_nothing(db, source_dir) -> None:
    add_performer_folder(source_dir, "D子")
    sid = _add_source(db, source_dir)
    scan_source(db, sid)
    before = {k: dict(v) for k, v in _rows(db).items()}
    with pytest.raises(SourceUnreachable):
        db.execute("UPDATE sources SET unc_path=? WHERE id=?", ("Z:\\no\\such\\dir\\", sid))
        scan_source(db, sid)
    after = {k: dict(v) for k, v in _rows(db).items()}
    assert before == after  # records untouched, D子 NOT marked missing


def test_hidden_and_files_ignored(db, source_dir, tmp_path) -> None:
    add_performer_folder(source_dir, "E子")
    (source_dir / ".hidden_dir").mkdir()
    (source_dir / "not_a_dir.txt").write_text("x")
    if sys.platform == "win32":
        hidden = source_dir / "SysHidden"
        hidden.mkdir()
        ctypes.windll.kernel32.SetFileAttributesW(str(hidden), 0x2)  # FILE_ATTRIBUTE_HIDDEN
    names = list_subdirs(str(source_dir))
    assert names == ["E子"]


def test_thumbnail_generated_and_mtime_skip(db, source_dir) -> None:
    d = add_performer_folder(source_dir, "F子", with_jpg=True)
    sid = _add_source(db, source_dir)
    scan_source(db, sid)
    row = _rows(db)["F子"]
    assert row["thumb"] is not None and row["thumb_mtime"] is not None
    img = Image.open(__import__("io").BytesIO(row["thumb"]))
    assert max(img.size) <= THUMB_MAX_SIDE and img.format == "JPEG"
    # second scan with unchanged mtime must not regenerate (same blob object)
    scan_source(db, sid)
    row2 = _rows(db)["F子"]
    assert row2["thumb"] == row["thumb"] and row2["thumb_mtime"] == row["thumb_mtime"]


def test_thumbnail_removed_jpg_clears_thumb(db, source_dir) -> None:
    d = add_performer_folder(source_dir, "G子", with_jpg=True)
    sid = _add_source(db, source_dir)
    scan_source(db, sid)
    (d / "folder.jpg").unlink()
    scan_source(db, sid)
    row = _rows(db)["G子"]
    assert row["thumb"] is None and row["thumb_mtime"] is None


def test_thumbnail_corrupt_jpg_keeps_old_value(db, source_dir) -> None:
    d = add_performer_folder(source_dir, "H子", with_jpg=True)
    sid = _add_source(db, source_dir)
    scan_source(db, sid)
    old = _rows(db)["H子"]["thumb"]
    jpg = d / "folder.jpg"
    jpg.write_bytes(b"not an image at all")
    scan_source(db, sid)  # must not raise, keeps old blob
    row = _rows(db)["H子"]
    assert row["thumb"] == old


def test_missing_record_thumb_untouched(db, source_dir) -> None:
    d = add_performer_folder(source_dir, "I子", with_jpg=True)
    sid = _add_source(db, source_dir)
    scan_source(db, sid)
    old = _rows(db)["I子"]["thumb"]
    import shutil

    shutil.rmtree(d)
    scan_source(db, sid)
    row = _rows(db)["I子"]
    assert row["is_missing"] == 1 and row["thumb"] == old


def test_case_only_rename_refreshes_same_record(db, source_dir) -> None:
    """Codex P2 regression: Windows/SMB is case-insensitive — a case-only
    rename must refresh the same record (adopting the new casing), not
    insert a duplicate + mark the original missing."""
    d = add_performer_folder(source_dir, "Alice")
    sid = _add_source(db, source_dir)
    scan_source(db, sid)
    old_id = _rows(db)["Alice"]["id"]

    d.rename(d.parent / "ALICE")
    r = scan_source(db, sid)
    assert (r.added, r.refreshed, r.missing) == (0, 1, 0)
    rows = _rows(db)
    assert list(rows) == ["ALICE"]          # single record, new casing
    assert rows["ALICE"]["id"] == old_id    # same identity
    assert rows["ALICE"]["is_missing"] == 0
    assert rows["ALICE"]["unc_path"].endswith("ALICE")


def test_scan_exception_rolls_back_partial_writes(db, source_dir, monkeypatch) -> None:
    """GLM P1 regression: an exception mid-scan must roll back the open
    transaction — otherwise a later unrelated commit on the shared
    connection persists the partial scan (silent corruption)."""
    import filmpaw_server.scan as scan_mod

    for i in range(3):
        add_performer_folder(source_dir, f"R{i}")
    sid = _add_source(db, source_dir)

    calls = {"n": 0}
    real = scan_mod._refresh_thumb

    def exploding(conn, pid, folder, cached, cached_side):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("simulated disk failure mid-scan")
        return real(conn, pid, folder, cached, cached_side)

    monkeypatch.setattr(scan_mod, "_refresh_thumb", exploding)
    with pytest.raises(RuntimeError):
        scan_source(db, sid)

    assert not db.in_transaction  # no dirty transaction left behind
    db.commit()  # an unrelated later commit must persist nothing partial
    assert db.execute("SELECT COUNT(*) c FROM performers").fetchone()["c"] == 0


def test_schema_version_mismatch_refuses_to_start(tmp_path) -> None:
    """GLM P2 regression: a mismatched schema_version must fail loudly
    instead of silently running against a legacy DB shape."""
    from filmpaw_server.db import connect

    path = tmp_path / "mismatch.db"
    conn = connect(path)
    conn.execute("UPDATE schema_version SET version = 999")
    conn.commit()
    conn.close()
    with pytest.raises(RuntimeError, match="schema version 999"):
        connect(path)
