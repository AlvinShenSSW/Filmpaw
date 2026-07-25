"""SQLite storage per design §4.

DB location: %APPDATA%\\Filmpaw\\library.db, overridable via FILMPAW_DB
(tests inject a temp path).
"""

import os
import sqlite3
from pathlib import Path

SCHEMA_VERSION = 1

DDL = """
CREATE TABLE IF NOT EXISTS schema_version (
  version INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS sources (
  id INTEGER PRIMARY KEY,
  unc_path TEXT NOT NULL UNIQUE,
  label TEXT,
  last_scan_at TEXT
);
CREATE TABLE IF NOT EXISTS performers (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  name_norm TEXT NOT NULL,
  source_id INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
  folder_name TEXT NOT NULL,
  unc_path TEXT NOT NULL UNIQUE,
  first_seen_at TEXT NOT NULL,
  last_seen_at TEXT NOT NULL,
  is_missing INTEGER NOT NULL DEFAULT 0,
  thumb BLOB,
  thumb_mtime REAL
);
CREATE INDEX IF NOT EXISTS idx_performers_name_norm ON performers(name_norm);
CREATE INDEX IF NOT EXISTS idx_performers_source ON performers(source_id);
CREATE INDEX IF NOT EXISTS idx_performers_is_missing ON performers(is_missing);
CREATE TABLE IF NOT EXISTS aliases (
  id INTEGER PRIMARY KEY,
  name_norm TEXT NOT NULL,
  alias TEXT NOT NULL,
  alias_norm TEXT NOT NULL,
  UNIQUE(name_norm, alias_norm)
);
CREATE INDEX IF NOT EXISTS idx_aliases_name_norm ON aliases(name_norm);
CREATE INDEX IF NOT EXISTS idx_aliases_alias_norm ON aliases(alias_norm);
CREATE TABLE IF NOT EXISTS settings (
  key TEXT PRIMARY KEY,
  value TEXT
);
"""


def default_db_path() -> Path:
    env = os.environ.get("FILMPAW_DB")
    if env:
        return Path(env)
    appdata = os.environ.get("APPDATA") or str(Path.home())
    return Path(appdata) / "Filmpaw" / "library.db"


def connect(db_path: Path | None = None) -> sqlite3.Connection:
    path = db_path or default_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    # FastAPI sync endpoints run in a threadpool, so the connection created in
    # lifespan is used from other threads. CPython's sqlite3 is built serialized
    # (threadsafety=3) so cross-thread sharing is safe.
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(DDL)
    cur = conn.execute("SELECT version FROM schema_version")
    row = cur.fetchone()
    if row is None:
        conn.execute("INSERT INTO schema_version(version) VALUES (?)", (SCHEMA_VERSION,))
        conn.commit()
    elif row["version"] != SCHEMA_VERSION:
        # CREATE TABLE IF NOT EXISTS would silently accept a mismatched
        # legacy DB — refuse to run against one instead.
        conn.close()
        raise RuntimeError(
            f"unsupported DB schema version {row['version']} (expected {SCHEMA_VERSION})"
        )
    return conn
