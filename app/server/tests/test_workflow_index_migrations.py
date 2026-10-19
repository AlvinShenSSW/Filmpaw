import sqlite3
from uuid import uuid4

import pytest

from filmpaw_server import db as dbmod
from filmpaw_server.workflow_schema import migrate_4_to_5


def v4(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(dbmod.DDL)
    conn.execute(dbmod.ARCHIVE_DDL)
    dbmod._migrate_3_to_4(conn)
    conn.execute("INSERT INTO schema_version VALUES(4)")
    conn.execute("INSERT INTO sources(id,unc_path,label) VALUES(1,?,?)", ("//example/share", "Example"))
    pid = str(uuid4())
    conn.execute("INSERT INTO performers(id,name,name_norm,source_id,folder_name,unc_path,"
                 "first_seen_at,last_seen_at,thumb,thumb_side) VALUES(?,?,?,?,?,?,?,?,?,?)",
                 (pid, "Example Actor", "example actor", 1, "Example Actor", "//example/share/Example Actor",
                  "2026-01-01", "2026-01-01", b"old-thumb", 256))
    conn.execute("INSERT INTO aliases(name_norm,alias,alias_norm) VALUES('example actor','Alias','alias')")
    conn.commit()
    conn.close()
    return pid


def test_v4_migration_is_offline_atomic_and_preserves_ids(tmp_path, monkeypatch):
    path = tmp_path / "library.db"
    pid = v4(path)
    with monkeypatch.context() as migration_guard:
        migration_guard.setattr("os.scandir", lambda *_: pytest.fail("migration must not enumerate NAS"))
        conn = dbmod.connect(path)
    assert conn.execute("SELECT version FROM schema_version").fetchone()[0] == dbmod.SCHEMA_VERSION
    root = conn.execute("SELECT * FROM workflow_roots").fetchone()
    location = conn.execute("SELECT * FROM workflow_locations").fetchone()
    assert root["legacy_source_id"] == 1 and root["identity_json"] is None
    assert location["location_id"] == pid and location["identity_json"] is None
    assert conn.execute("SELECT thumb FROM performers WHERE id=?", (pid,)).fetchone()[0] == b"old-thumb"
    library = conn.execute("SELECT library_id FROM workflow_index_meta").fetchone()[0]
    conn.close()
    backups = list(tmp_path.glob("library.db.pre-v5-*.sqlite3"))
    assert len(backups) == 1
    backup = sqlite3.connect(backups[0])
    assert backup.execute("SELECT version FROM schema_version").fetchone()[0] == 4
    backup.close()
    with monkeypatch.context() as migration_guard:
        migration_guard.setattr("os.scandir", lambda *_: pytest.fail("reopen must not enumerate NAS"))
        conn = dbmod.connect(path)
    assert conn.execute("SELECT library_id FROM workflow_index_meta").fetchone()[0] == library
    conn.close()


def test_migration_failure_leaves_v4_no_half_workflow_tables(tmp_path, monkeypatch):
    path = tmp_path / "library.db"
    pid = v4(path)
    def crash(conn):
        migrate_4_to_5(conn)
        raise RuntimeError("simulated commit boundary failure")
    monkeypatch.setitem(dbmod._MIGRATIONS, 4, crash)
    with pytest.raises(RuntimeError):
        dbmod.connect(path)
    raw = sqlite3.connect(path)
    assert raw.execute("SELECT version FROM schema_version").fetchone()[0] == 4
    assert raw.execute("SELECT id FROM performers").fetchone()[0] == pid
    assert not raw.execute("SELECT name FROM sqlite_master WHERE name='workflow_roots'").fetchall()
    raw.close()


def test_fresh_schema_and_future_schema_refusal(tmp_path):
    path = tmp_path / "fresh.db"
    conn = dbmod.connect(path)
    assert conn.execute("SELECT index_revision FROM workflow_index_meta").fetchone()[0] == 1
    conn.execute("UPDATE schema_version SET version=999")
    conn.execute("DROP TABLE workflow_roots")
    conn.commit()
    conn.close()
    with pytest.raises(RuntimeError):
        dbmod.connect(path)
    raw = sqlite3.connect(path)
    assert not raw.execute("SELECT name FROM sqlite_master WHERE name='workflow_roots'").fetchall()
    raw.close()


def test_real_commit_method_failure_keeps_version_and_backup(tmp_path, monkeypatch):
    path = tmp_path / "commit.db"
    pid = v4(path)
    original = sqlite3.connect
    class CommitFailure(sqlite3.Connection):
        def commit(self):
            version = self.execute("SELECT version FROM schema_version").fetchone()[0]
            if version == 5:
                raise sqlite3.OperationalError("synthetic commit failure")
            return super().commit()
    def connect(*args, **kwargs):
        if args[0] == path:
            kwargs["factory"] = CommitFailure
        return original(*args, **kwargs)
    monkeypatch.setattr(dbmod.sqlite3, "connect", connect)
    with pytest.raises(sqlite3.OperationalError):
        dbmod.connect(path)
    raw = original(path)
    assert raw.execute("SELECT version FROM schema_version").fetchone()[0] == 4
    assert raw.execute("SELECT id FROM performers").fetchone()[0] == pid
    raw.close()
    assert list(tmp_path.glob("commit.db.pre-v5-*.sqlite3"))
