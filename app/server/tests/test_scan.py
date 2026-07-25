import ctypes
import sqlite3
import sys
from pathlib import Path

import pytest
from PIL import Image

from filmpaw_server.scan import SourceUnreachable, list_subdirs, scan_source
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
    assert max(img.size) <= 256 and img.format == "JPEG"
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
