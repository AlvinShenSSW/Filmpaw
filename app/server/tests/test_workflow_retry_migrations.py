"""Real candidate-v5 databases migrate root-specific retry state atomically."""

import json
import sqlite3
from uuid import uuid4

import pytest

from filmpaw_server import db as dbmod
from filmpaw_server.workflow_schema import UNKNOWN_HOST_ID, migrate_4_to_5
from tests.test_workflow_index_migrations import v4


def v5(path):
    performer = v4(path)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    migrate_4_to_5(conn)
    conn.execute("UPDATE schema_version SET version=5")
    broken = conn.execute("SELECT root_id FROM workflow_roots").fetchone()[0]
    conn.execute("UPDATE workflow_roots SET health='permission_denied',probe_health='permission_denied' WHERE root_id=?", (broken,))
    healthy = str(uuid4())
    typo = str(uuid4())
    for root, label in ((healthy, "Healthy"), (typo, "Typo")):
        conn.execute("INSERT INTO workflow_roots(root_id,host_id,export_id,private_path,label) VALUES(?,?,?,?,?)",
                     (root, UNKNOWN_HOST_ID, str(uuid4()), "//example/" + label, label))
    conn.execute("UPDATE workflow_host_state SET configuration_required=1")
    conn.execute("UPDATE workflow_roots SET health='partial',probe_health='partial' WHERE root_id=?", (typo,))
    # Historical v5 outcomes and queues are migration fixtures, not new business credentials.
    ids = {}
    for root, state, error in ((broken, "queued", None), (healthy, "queued", None), (typo, "failed", "not_directory")):
        scan, attempt = str(uuid4()), str(uuid4())
        ids[root] = (scan, attempt)
        conn.execute("INSERT INTO workflow_scan_operations(scan_id,request_id,attempt_id,root_id,host_id,root_revision,mapping_revision,kind,state,owner_state,created_at,outcome_json,completed_at) VALUES(?,?,?,?,?,1,1,'probe_root',?,?,?, ?,?)",
                     (scan, str(uuid4()), attempt, root, UNKNOWN_HOST_ID, state,
                      "released" if error else "none", "2026-10-19T00:00:00+00:00",
                      json.dumps({"error_code": error}) if error else None,
                      "2026-10-19T00:00:01+00:00" if error else None))
    library = conn.execute("SELECT library_id FROM workflow_index_meta").fetchone()[0]
    conn.commit()
    conn.close()
    return performer, library, broken, healthy, typo, ids


def test_v5_migration_is_root_specific_keeps_facts_and_backs_up(tmp_path):
    path = tmp_path / "candidate.db"
    performer, library, broken, healthy, typo, ids = v5(path)
    conn = dbmod.connect(path)
    assert conn.execute("SELECT version FROM schema_version").fetchone()[0] == 6
    assert conn.execute("SELECT library_id FROM workflow_index_meta").fetchone()[0] == library
    assert conn.execute("SELECT id FROM performers").fetchone()[0] == performer
    reasons = dict(conn.execute("SELECT root_id,configuration_reason FROM workflow_roots"))
    assert reasons == {broken: "permission_denied", healthy: None, typo: "not_directory"}
    assert conn.execute("SELECT configuration_required FROM workflow_host_state").fetchone()[0] == 0
    rows = {r["root_id"]: r for r in conn.execute("SELECT * FROM workflow_scan_operations")}
    assert rows[broken]["state"] == "cancelled" and rows[broken]["publication_allowed"] == 0
    assert rows[healthy]["state"] == "queued"
    assert all(r["manual_intent"] == 0 for r in rows.values())
    assert rows[typo]["scan_id"] == ids[typo][0]
    conn.close()
    backups = list(tmp_path.glob("candidate.db.pre-v6-*.sqlite3"))
    assert len(backups) == 1
    with sqlite3.connect(backups[0]) as backup:
        assert backup.execute("SELECT version FROM schema_version").fetchone()[0] == 5
        assert "configuration_reason" not in [r[1] for r in backup.execute("PRAGMA table_info(workflow_roots)")]
    conn = dbmod.connect(path)
    assert conn.execute("SELECT library_id FROM workflow_index_meta").fetchone()[0] == library
    conn.close()
    assert len(list(tmp_path.glob("candidate.db.pre-v6-*.sqlite3"))) == 1


@pytest.mark.parametrize("failure", ["ddl", "commit"])
def test_v5_upgrade_failure_is_atomic_and_retryable(tmp_path, monkeypatch, failure):
    path = tmp_path / "candidate.db"
    _, library, *_ = v5(path)
    original_connect = sqlite3.connect
    if failure == "ddl":
        original = dbmod._MIGRATIONS.get(5)
        assert original is not None
        def crash(conn):
            original(conn)
            raise RuntimeError("synthetic after DDL")
        monkeypatch.setitem(dbmod._MIGRATIONS, 5, crash)
        expected = RuntimeError
    else:
        class FailCommit(sqlite3.Connection):
            def commit(self):
                if self.execute("SELECT version FROM schema_version").fetchone()[0] == 6:
                    raise sqlite3.OperationalError("synthetic commit")
                return super().commit()
        def connect(*args, **kwargs):
            if args[0] == path:
                kwargs["factory"] = FailCommit
            return original_connect(*args, **kwargs)
        monkeypatch.setattr(dbmod.sqlite3, "connect", connect)
        expected = sqlite3.OperationalError
    with pytest.raises(expected):
        dbmod.connect(path)
    with original_connect(path) as raw:
        assert raw.execute("SELECT version FROM schema_version").fetchone()[0] == 5
        assert raw.execute("SELECT library_id FROM workflow_index_meta").fetchone()[0] == library
        assert "configuration_reason" not in [r[1] for r in raw.execute("PRAGMA table_info(workflow_roots)")]
        assert raw.execute("SELECT configuration_required FROM workflow_host_state").fetchone()[0] == 1
    assert list(tmp_path.glob("candidate.db.pre-v6-*.sqlite3"))
    monkeypatch.undo()
    conn = dbmod.connect(path)
    assert conn.execute("SELECT version FROM schema_version").fetchone()[0] == 6
    conn.close()
