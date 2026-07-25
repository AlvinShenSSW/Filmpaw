"""Scan engine per design §5.

scan(conn, source_id):
  unreachable source  -> SOURCE_UNREACHABLE, no records touched
  new folder          -> new performer row (uuid, now, is_missing=0)
  existing folder     -> refresh last_seen_at, clear is_missing
                         (casefold match: Windows/SMB is case-insensitive,
                         a case-only rename adopts the new casing)
  gone folder         -> is_missing=1 (never delete)
  thumbnails (D9)     -> folder.jpg -> <=256px JPEG q80 blob, keyed by mtime;
                         removed jpg clears thumb; unreadable jpg keeps the
                         old value and logs a warning; missing records are
                         left untouched
Hidden/system entries are ignored. All writes commit atomically; any
exception rolls the transaction back so a partial scan can never be
committed later by an unrelated request on the shared connection.
"""

import io
import logging
import os
import sqlite3
import stat
import sys
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from PIL import Image

from filmpaw_server.normalize import normalize

log = logging.getLogger(__name__)

THUMB_MAX_SIDE = 256
THUMB_QUALITY = 80


class SourceUnreachable(Exception):
    pass


@dataclass
class ScanResult:
    added: int = 0
    refreshed: int = 0
    missing: int = 0


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _is_hidden(entry: os.DirEntry) -> bool:
    if entry.name.startswith("."):
        return True
    if sys.platform == "win32":
        try:
            attrs = entry.stat(follow_symlinks=False).st_file_attributes
            return bool(attrs & (stat.FILE_ATTRIBUTE_HIDDEN | stat.FILE_ATTRIBUTE_SYSTEM))
        except OSError:
            return True
    return False


def list_subdirs(root: str) -> list[str]:
    """First-level visible directory names under root. Raises
    SourceUnreachable when root is not a listable directory."""
    if not os.path.isdir(root):
        raise SourceUnreachable(root)
    try:
        with os.scandir(root) as it:
            return sorted(
                e.name for e in it if e.is_dir(follow_symlinks=False) and not _is_hidden(e)
            )
    except OSError as e:
        raise SourceUnreachable(root) from e


def make_thumb(jpg_path: Path) -> bytes:
    with Image.open(jpg_path) as img:
        # Let the JPEG decoder downscale during read — avoids materializing
        # the full-resolution bitmap before thumbnailing.
        img.draft("RGB", (THUMB_MAX_SIDE, THUMB_MAX_SIDE))
        img = img.convert("RGB")
        img.thumbnail((THUMB_MAX_SIDE, THUMB_MAX_SIDE))
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=THUMB_QUALITY)
        return buf.getvalue()


def _refresh_thumb(
    conn: sqlite3.Connection, performer_id: str, folder: Path, cached_mtime: float | None
) -> None:
    """cached_mtime comes from the single per-source SELECT (no N+1)."""
    jpg = folder / "folder.jpg"
    try:
        mtime = jpg.stat().st_mtime
    except OSError:
        # folder.jpg gone -> clear thumb (D9); skip the write when there was
        # nothing stored anyway.
        if cached_mtime is not None:
            conn.execute(
                "UPDATE performers SET thumb=NULL, thumb_mtime=NULL WHERE id=?",
                (performer_id,),
            )
        return
    if cached_mtime == mtime:
        return  # unchanged, skip regen
    try:
        blob = make_thumb(jpg)
    except Exception as e:  # unreadable/corrupt image: keep old value, warn
        log.warning("thumbnail failed for %s: %s", jpg, e)
        return
    conn.execute(
        "UPDATE performers SET thumb=?, thumb_mtime=? WHERE id=?",
        (blob, mtime, performer_id),
    )


def scan_source(conn: sqlite3.Connection, source_id: int) -> ScanResult:
    src = conn.execute("SELECT * FROM sources WHERE id=?", (source_id,)).fetchone()
    if src is None:
        raise KeyError(source_id)
    root = src["unc_path"]
    disk_names = list_subdirs(root)  # raises SourceUnreachable before any write

    now = _now()
    result = ScanResult()
    try:
        db_rows = conn.execute(
            "SELECT id, folder_name, thumb_mtime, is_missing FROM performers"
            " WHERE source_id=?",
            (source_id,),
        ).fetchall()
        # Windows/SMB is case-insensitive: match on casefold so a case-only
        # rename (Alice -> ALICE) refreshes the same record instead of
        # inserting a duplicate and marking the original missing.
        by_folder = {r["folder_name"].casefold(): r for r in db_rows}
        disk_set = {n.casefold() for n in disk_names}

        for name in disk_names:
            folder_path = str(Path(root) / name)
            key = name.casefold()
            row = by_folder.get(key)
            if row is not None:
                pid = row["id"]
                cached_mtime = row["thumb_mtime"]
                conn.execute(
                    "UPDATE performers SET last_seen_at=?, is_missing=0 WHERE id=?",
                    (now, pid),
                )
                if row["folder_name"] != name:  # case-only rename: adopt new casing
                    conn.execute(
                        "UPDATE performers SET name=?, name_norm=?, folder_name=?,"
                        " unc_path=? WHERE id=?",
                        (name, normalize(name), name, folder_path, pid),
                    )
                result.refreshed += 1
            else:
                pid = str(uuid.uuid4())
                cached_mtime = None
                conn.execute(
                    "INSERT INTO performers(id, name, name_norm, source_id, folder_name,"
                    " unc_path, first_seen_at, last_seen_at, is_missing)"
                    " VALUES (?,?,?,?,?,?,?,?,0)",
                    (pid, name, normalize(name), source_id, name, folder_path, now, now),
                )
                result.added += 1
            _refresh_thumb(conn, pid, Path(root) / name, cached_mtime)

        for key, row in by_folder.items():
            if key not in disk_set and not row["is_missing"]:
                conn.execute("UPDATE performers SET is_missing=1 WHERE id=?", (row["id"],))
                # Count NEWLY missing only, matching the added/refreshed
                # delta semantics of the scan summary.
                result.missing += 1

        conn.execute("UPDATE sources SET last_scan_at=? WHERE id=?", (now, source_id))
        conn.commit()
    except Exception:
        # Never leave a dirty transaction on the shared connection — a later
        # unrelated commit would persist the partial scan (silent corruption).
        conn.rollback()
        raise
    return result
