"""F01 v5 DDL and offline legacy association. Called inside the owning transaction."""

import sqlite3
from datetime import datetime, timezone
from uuid import UUID, uuid4, uuid5, NAMESPACE_URL

UNKNOWN_HOST_ID = str(uuid5(NAMESPACE_URL, "filmpaw:unverified-host-group"))

STATEMENTS = (
    """CREATE TABLE workflow_index_meta (
      singleton INTEGER PRIMARY KEY CHECK(singleton=1), library_id TEXT NOT NULL UNIQUE,
      index_revision INTEGER NOT NULL CHECK(index_revision>0), schema_version INTEGER NOT NULL)""",
    """CREATE TABLE workflow_host_state (
      host_id TEXT PRIMARY KEY, host_label TEXT NOT NULL, generation INTEGER NOT NULL DEFAULT 0,
      failures INTEGER NOT NULL DEFAULT 0, circuit TEXT NOT NULL DEFAULT 'closed',
      next_probe_at TEXT, configuration_required INTEGER NOT NULL DEFAULT 0)""",
    """CREATE TABLE workflow_roots (
      root_id TEXT PRIMARY KEY, legacy_source_id INTEGER UNIQUE REFERENCES sources(id) ON DELETE SET NULL,
      host_id TEXT NOT NULL REFERENCES workflow_host_state(host_id), export_id TEXT NOT NULL,
      private_path TEXT NOT NULL, label TEXT NOT NULL, root_revision INTEGER NOT NULL DEFAULT 1,
      mapping_revision INTEGER NOT NULL DEFAULT 1, active INTEGER NOT NULL DEFAULT 0,
      tombstoned INTEGER NOT NULL DEFAULT 0, identity_json TEXT, health TEXT NOT NULL DEFAULT 'never_scanned',
      last_attempt_at TEXT, last_success_at TEXT, probe_observed_at TEXT,
      probe_health TEXT NOT NULL DEFAULT 'never_scanned', observed_at TEXT)""",
    """CREATE TABLE workflow_locations (
      location_id TEXT PRIMARY KEY, root_id TEXT NOT NULL REFERENCES workflow_roots(root_id) ON DELETE RESTRICT,
      legacy_performer_id TEXT REFERENCES performers(id) ON DELETE SET NULL,
      relative_path TEXT NOT NULL, path_key TEXT NOT NULL, name TEXT NOT NULL, name_norm TEXT NOT NULL,
      identity_json TEXT, first_seen_at TEXT NOT NULL, last_seen_at TEXT NOT NULL,
      state TEXT NOT NULL DEFAULT 'unknown', revision INTEGER NOT NULL DEFAULT 1,
      observation_id TEXT NOT NULL, retired INTEGER NOT NULL DEFAULT 0)""",
    "CREATE INDEX workflow_locations_root ON workflow_locations(root_id,path_key)",
    "CREATE INDEX workflow_locations_names ON workflow_locations(name_norm)",
    """CREATE TABLE workflow_location_evidence (
      evidence_id TEXT PRIMARY KEY, location_id TEXT NOT NULL REFERENCES workflow_locations(location_id),
      revision INTEGER NOT NULL, kind TEXT NOT NULL, value_norm TEXT NOT NULL,
      body_json TEXT NOT NULL, observed_at TEXT NOT NULL)""",
    """CREATE TABLE workflow_film_observations (
      observation_id TEXT PRIMARY KEY, root_id TEXT NOT NULL REFERENCES workflow_roots(root_id),
      location_id TEXT NOT NULL REFERENCES workflow_locations(location_id),
      revision INTEGER NOT NULL, relative_path TEXT NOT NULL, body_json TEXT NOT NULL,
      observed_at TEXT NOT NULL, current INTEGER NOT NULL DEFAULT 1)""",
    """CREATE TABLE workflow_scan_operations (
      scan_id TEXT PRIMARY KEY, request_id TEXT NOT NULL UNIQUE, attempt_id TEXT NOT NULL UNIQUE,
      root_id TEXT NOT NULL REFERENCES workflow_roots(root_id), host_id TEXT NOT NULL,
      root_revision INTEGER NOT NULL, mapping_revision INTEGER NOT NULL,
      generation INTEGER NOT NULL DEFAULT 0, kind TEXT NOT NULL,
      state TEXT NOT NULL, owner_state TEXT NOT NULL DEFAULT 'none',
      publication_allowed INTEGER NOT NULL DEFAULT 1, request_json TEXT, owner_json TEXT,
      outcome_json TEXT, finish_json TEXT, created_at TEXT NOT NULL, completed_at TEXT)""",
    """CREATE UNIQUE INDEX workflow_host_owner ON workflow_scan_operations(host_id)
      WHERE owner_state IN ('owned','unknown_owner')""",
    """CREATE UNIQUE INDEX workflow_root_queued ON workflow_scan_operations(root_id)
      WHERE state='queued'""",
)


def ensure_legacy_roots(conn: sqlite3.Connection) -> None:
    """Associate existing rows without probing a path or changing a legacy ID."""
    conn.execute("INSERT OR IGNORE INTO workflow_host_state(host_id,host_label) VALUES(?,?)",
                 (UNKNOWN_HOST_ID, "Unverified NAS"))
    for src in conn.execute("SELECT * FROM sources WHERE id NOT IN "
                            "(SELECT legacy_source_id FROM workflow_roots WHERE legacy_source_id IS NOT NULL)").fetchall():
        conn.execute("INSERT INTO workflow_roots(root_id,legacy_source_id,host_id,export_id,private_path,label,active)"
                     " VALUES(?,?,?,?,?,?,1)", (str(uuid4()), src["id"], UNKNOWN_HOST_ID, str(uuid4()),
                                                src["unc_path"], src["label"] or "NAS"))
    for row in conn.execute("SELECT p.*,r.root_id FROM performers p JOIN workflow_roots r ON r.legacy_source_id=p.source_id "
                            "WHERE p.id NOT IN (SELECT legacy_performer_id FROM workflow_locations "
                            "WHERE legacy_performer_id IS NOT NULL)").fetchall():
        try:
            location_id = str(UUID(row["id"]))
        except ValueError:
            location_id = str(uuid4())
        conn.execute("INSERT INTO workflow_locations(location_id,root_id,legacy_performer_id,relative_path,path_key,"
                     "name,name_norm,first_seen_at,last_seen_at,observation_id) VALUES(?,?,?,?,?,?,?,?,?,?)",
                     (location_id, row["root_id"], row["id"], row["folder_name"], row["folder_name"].casefold(),
                      row["name"], row["name_norm"], row["first_seen_at"], row["last_seen_at"], str(uuid4())))


def migrate_4_to_5(conn: sqlite3.Connection) -> None:
    for statement in STATEMENTS:
        conn.execute(statement)
    conn.execute("INSERT INTO workflow_index_meta VALUES(1,?,1,1)", (str(uuid4()),))
    ensure_legacy_roots(conn)
