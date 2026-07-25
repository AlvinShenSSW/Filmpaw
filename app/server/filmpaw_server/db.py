"""SQLite storage per design §4.

DB location: %APPDATA%\\Filmpaw\\library.db, overridable via FILMPAW_DB
(tests inject a temp path).
"""

import os
import sqlite3
from pathlib import Path

SCHEMA_VERSION = 2

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
  thumb_mtime REAL,
  thumb_side INTEGER
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

    # Read the version BEFORE touching the file: a DB written by a future
    # build must be refused untouched. Running our DDL first could recreate
    # objects that a newer schema had dropped.
    existing = _read_version(conn)
    if existing is not None and existing > SCHEMA_VERSION:
        conn.close()
        raise RuntimeError(
            f"unsupported DB schema version {existing} (expected {SCHEMA_VERSION})"
        )

    conn.executescript(DDL)
    if existing is None:
        if _read_version(conn) is None:
            conn.execute("INSERT INTO schema_version(version) VALUES (?)", (SCHEMA_VERSION,))
            conn.commit()
    elif existing != SCHEMA_VERSION:
        _migrate(conn, existing)
    return conn


def _read_version(conn: sqlite3.Connection) -> int | None:
    """Current schema version, or None for a fresh/unversioned database."""
    try:
        row = conn.execute("SELECT version FROM schema_version").fetchone()
    except sqlite3.OperationalError:  # table does not exist yet
        return None
    return row["version"] if row is not None else None


def _migrate(conn: sqlite3.Connection, from_version: int) -> None:
    """Upgrade a known older schema in place.

    Each step runs as ONE transaction covering both the DDL and the version
    bump: executescript() above implicitly commits, so an ALTER that landed
    without its version write would replay on the next start and die with
    "duplicate column name", bricking the library. Unknown/newer versions are
    still refused rather than guessed at.
    """
    if from_version > SCHEMA_VERSION:
        # Written by a NEWER build — downgrading is not something we can guess.
        conn.close()
        raise RuntimeError(
            f"unsupported DB schema version {from_version} (expected {SCHEMA_VERSION})"
        )
    version = from_version
    while version < SCHEMA_VERSION:
        step = _MIGRATIONS.get(version)
        if step is None:
            conn.close()
            raise RuntimeError(
                f"unsupported DB schema version {from_version} (expected {SCHEMA_VERSION})"
            )
        try:
            conn.execute("BEGIN")
            step(conn)
            conn.execute("UPDATE schema_version SET version = ?", (version + 1,))
            conn.commit()
        except Exception:
            conn.rollback()
            conn.close()
            raise
        version += 1


def _migrate_1_to_2(conn: sqlite3.Connection) -> None:
    """v2 records the max side each thumbnail was generated at, so raising
    THUMB_MAX_SIDE rebuilds existing thumbnails (mtime alone would skip them).
    Existing blobs came from the 256px era — record that explicitly instead of
    leaving NULL, so the rebuild predicate is a plain inequality for them."""
    conn.execute("ALTER TABLE performers ADD COLUMN thumb_side INTEGER")
    conn.execute("UPDATE performers SET thumb_side = 256 WHERE thumb IS NOT NULL")


_MIGRATIONS = {1: _migrate_1_to_2}
