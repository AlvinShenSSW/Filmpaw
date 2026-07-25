"""#27: schema v1 -> v2 migration and thumbnail rebuild at the new max side."""

import io
import sqlite3

import pytest
from PIL import Image

from filmpaw_server.db import SCHEMA_VERSION, connect
from filmpaw_server.scan import THUMB_MAX_SIDE, scan_source
from tests.conftest import add_performer_folder

V1_DDL = """
CREATE TABLE schema_version (version INTEGER NOT NULL);
CREATE TABLE sources (
  id INTEGER PRIMARY KEY, unc_path TEXT NOT NULL UNIQUE, label TEXT, last_scan_at TEXT
);
CREATE TABLE performers (
  id TEXT PRIMARY KEY, name TEXT NOT NULL, name_norm TEXT NOT NULL,
  source_id INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
  folder_name TEXT NOT NULL, unc_path TEXT NOT NULL UNIQUE,
  first_seen_at TEXT NOT NULL, last_seen_at TEXT NOT NULL,
  is_missing INTEGER NOT NULL DEFAULT 0, thumb BLOB, thumb_mtime REAL
);
CREATE TABLE aliases (
  id INTEGER PRIMARY KEY, name_norm TEXT NOT NULL, alias TEXT NOT NULL,
  alias_norm TEXT NOT NULL, UNIQUE(name_norm, alias_norm)
);
CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT);
"""


def _make_v1_db(path, source_root, folder_name="旧人", with_thumb=True):
    """A pre-migration library: v1 schema, a 256px thumbnail, no thumb_side."""
    conn = sqlite3.connect(path)
    conn.executescript(V1_DDL)
    conn.execute("INSERT INTO schema_version(version) VALUES (1)")
    conn.execute("INSERT INTO sources(id, unc_path, label) VALUES (1, ?, 'old')", (str(source_root),))
    blob = None
    mtime = None
    if with_thumb:
        img = Image.new("RGB", (170, 256), (180, 100, 40))  # max side 256 (old era)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=80)
        blob = buf.getvalue()
        mtime = (source_root / folder_name / "folder.jpg").stat().st_mtime
    conn.execute(
        "INSERT INTO performers(id, name, name_norm, source_id, folder_name, unc_path,"
        " first_seen_at, last_seen_at, is_missing, thumb, thumb_mtime)"
        " VALUES ('old-1', ?, ?, 1, ?, ?, '2026-01-01', '2026-01-01', 0, ?, ?)",
        (folder_name, folder_name, folder_name, str(source_root / folder_name), blob, mtime),
    )
    conn.commit()
    conn.close()


def test_v1_library_migrates_instead_of_refusing(tmp_path, source_dir) -> None:
    add_performer_folder(source_dir, "旧人", with_jpg=True)
    db = tmp_path / "old.db"
    _make_v1_db(db, source_dir)

    conn = connect(db)  # must NOT raise
    assert conn.execute("SELECT version FROM schema_version").fetchone()["version"] == SCHEMA_VERSION
    row = conn.execute("SELECT thumb_side FROM performers WHERE id='old-1'").fetchone()
    assert row["thumb_side"] == 256  # existing blob accounted for, not NULL
    conn.close()


def test_migration_is_idempotent_across_restarts(tmp_path, source_dir) -> None:
    add_performer_folder(source_dir, "旧人", with_jpg=True)
    db = tmp_path / "old.db"
    _make_v1_db(db, source_dir)
    connect(db).close()
    connect(db).close()  # second start must be a no-op, not "duplicate column"


def test_interrupted_migration_leaves_v1_intact_and_retries(tmp_path, source_dir, monkeypatch) -> None:
    """Atomicity: if the version bump never lands, the ALTER must roll back so
    the next start migrates cleanly instead of dying on a duplicate column."""
    import filmpaw_server.db as dbmod

    add_performer_folder(source_dir, "旧人", with_jpg=True)
    db = tmp_path / "old.db"
    _make_v1_db(db, source_dir)

    real = dbmod._migrate_1_to_2

    def crash(conn):
        real(conn)
        raise RuntimeError("power loss mid-migration")

    monkeypatch.setattr(dbmod, "_MIGRATIONS", {1: crash})
    with pytest.raises(RuntimeError, match="power loss"):
        connect(db)

    raw = sqlite3.connect(db)
    assert raw.execute("SELECT version FROM schema_version").fetchone()[0] == 1
    cols = [r[1] for r in raw.execute("PRAGMA table_info(performers)")]
    assert "thumb_side" not in cols  # rolled back
    raw.close()

    monkeypatch.setattr(dbmod, "_MIGRATIONS", {1: real})
    conn = connect(db)  # retry succeeds
    assert conn.execute("SELECT version FROM schema_version").fetchone()["version"] == SCHEMA_VERSION
    conn.close()


def test_scan_rebuilds_old_thumbnail_at_new_max_side(tmp_path, source_dir) -> None:
    """The whole point of thumb_side: an unchanged folder.jpg must still be
    re-thumbnailed after THUMB_MAX_SIDE grows."""
    add_performer_folder(source_dir, "旧人", with_jpg=True)
    db = tmp_path / "old.db"
    _make_v1_db(db, source_dir)

    conn = connect(db)
    before = conn.execute("SELECT thumb, thumb_side FROM performers WHERE id='old-1'").fetchone()
    assert max(Image.open(io.BytesIO(before["thumb"])).size) == 256

    scan_source(conn, 1)  # folder.jpg untouched — only the target size changed

    after = conn.execute("SELECT thumb, thumb_side FROM performers WHERE id='old-1'").fetchone()
    assert after["thumb_side"] == THUMB_MAX_SIDE
    assert max(Image.open(io.BytesIO(after["thumb"])).size) == THUMB_MAX_SIDE == 512
    conn.close()


def test_unknown_future_version_is_still_refused(tmp_path) -> None:
    db = tmp_path / "future.db"
    conn = connect(db)
    conn.execute("UPDATE schema_version SET version = 999")
    conn.commit()
    conn.close()
    with pytest.raises(RuntimeError, match="999"):
        connect(db)


def test_thumb_etag_changes_when_size_changes(tmp_path, source_dir) -> None:
    """Kimi P2: the rebuilt 512 blob must not be masked by a cached 256 one.
    folder.jpg's mtime is unchanged across the upgrade, so an mtime-only ETag
    would let browsers keep the old image forever."""
    from fastapi.testclient import TestClient

    from filmpaw_server.app import create_app

    add_performer_folder(source_dir, "旧人", with_jpg=True)
    db = tmp_path / "old.db"
    _make_v1_db(db, source_dir)

    with TestClient(create_app(db_path=db)) as c:
        before = c.get("/api/performers/old-1/thumb")
        assert before.status_code == 200
        etag_256 = before.headers["etag"]

        sid = c.get("/api/sources").json()[0]["id"]
        c.post(f"/api/sources/{sid}/scan")  # rebuilds at 512, mtime untouched

        after = c.get("/api/performers/old-1/thumb")
        assert after.headers["etag"] != etag_256
        assert max(Image.open(io.BytesIO(after.content)).size) == THUMB_MAX_SIDE

        # must revalidate every time — max-age would serve the pre-upgrade
        # 256px image from cache for up to a day without ever asking
        assert "no-cache" in after.headers["cache-control"]
        # a stale tag gets the new bytes, the current tag short-circuits to 304
        assert c.get(
            "/api/performers/old-1/thumb", headers={"If-None-Match": etag_256}
        ).status_code == 200
        revalidated = c.get(
            "/api/performers/old-1/thumb", headers={"If-None-Match": after.headers["etag"]}
        )
        assert revalidated.status_code == 304 and not revalidated.content


def test_future_version_db_is_left_untouched(tmp_path) -> None:
    """Kimi P2: refuse a newer schema BEFORE running our DDL — replaying old
    DDL over a future schema could recreate objects it had dropped."""
    db = tmp_path / "future.db"
    conn = connect(db)
    conn.execute("UPDATE schema_version SET version = 999")
    conn.execute("DROP TABLE settings")  # pretend a future schema removed it
    conn.commit()
    conn.close()

    with pytest.raises(RuntimeError, match="999"):
        connect(db)

    raw = sqlite3.connect(db)
    tables = {r[0] for r in raw.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "settings" not in tables  # our DDL did not resurrect it
    raw.close()


def test_weak_and_list_etags_still_short_circuit(tmp_path, source_dir) -> None:
    """Kimi P2: If-None-Match may be weak or a list (RFC 7232)."""
    from fastapi.testclient import TestClient

    from filmpaw_server.app import create_app

    add_performer_folder(source_dir, "缓存", with_jpg=True)
    db = tmp_path / "c.db"
    with TestClient(create_app(db_path=db)) as c:
        sid = c.post("/api/sources", json={"unc_path": str(source_dir)}).json()["id"]
        c.post(f"/api/sources/{sid}/scan")
        pid = c.get("/api/performers").json()["items"][0]["id"]
        etag = c.get(f"/api/performers/{pid}/thumb").headers["etag"]

        for header in [etag, f"W/{etag}", f'"other", {etag}', "*"]:
            r = c.get(f"/api/performers/{pid}/thumb", headers={"If-None-Match": header})
            assert r.status_code == 304, header
        assert c.get(
            f"/api/performers/{pid}/thumb", headers={"If-None-Match": '"stale"'}
        ).status_code == 200
