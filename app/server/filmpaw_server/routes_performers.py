"""Performers / aliases / search / open endpoints per design §6 and issue #3.

Contract highlights:
- GET /api/performers returns the pagination envelope {items, total, page,
  page_size, source_count}; page is 1-based, page_size default 50 max 200
  (422 above), stable sort name_norm ASC then id ASC; total/source_count
  are computed on the filtered set ignoring pagination.
- D5 group-level aliases: aliases live on name_norm. A query hit on an
  alias surfaces every record of the same-name group; every record of a
  group lists the same aliases.
- All handlers serialize on app.state.db_lock (shared sqlite connection).
"""

import os
import sqlite3
import subprocess
import sys

from fastapi import APIRouter, HTTPException, Query, Request, Response
from pydantic import BaseModel

from filmpaw_server.normalize import normalize
from filmpaw_server.scan import SourceUnreachable, list_subdirs

router = APIRouter(prefix="/api")

PAGE_SIZE_DEFAULT = 50
PAGE_SIZE_MAX = 200


class AliasOut(BaseModel):
    id: int
    alias: str


class PerformerOut(BaseModel):
    id: str
    name: str
    source_id: int
    source_label: str | None
    unc_path: str
    is_missing: bool
    has_thumb: bool
    aliases: list[AliasOut]


class PerformerListOut(BaseModel):
    items: list[PerformerOut]
    total: int
    page: int
    page_size: int
    source_count: int
    missing_total: int  # GLOBAL missing count (filter-independent) — purge scope


class PurgeOut(BaseModel):
    deleted: int


class SettingsOut(BaseModel):
    last_local_dir: str | None
    db_path: str


class SubdirsOut(BaseModel):
    path: str
    subdirs: list[str]




def _conn(request: Request) -> sqlite3.Connection:
    return request.app.state.db


def _lock(request: Request):
    return request.app.state.db_lock


def open_in_explorer(path: str) -> None:
    """Open a folder in the OS file manager. Split out for test injection."""
    if sys.platform == "win32":
        subprocess.Popen(["explorer", path])
    else:  # dev convenience on other platforms
        subprocess.Popen(["xdg-open" if sys.platform.startswith("linux") else "open", path])


# ---------------------------------------------------------------- list/search

_MATCH_SQL = """
WITH matched_groups AS (
  SELECT DISTINCT name_norm FROM performers
   WHERE name_norm LIKE '%' || :q_like || '%' ESCAPE '\\'
      OR (LENGTH(name_norm) >= 2 AND INSTR(:q_raw, name_norm) > 0)
  UNION
  SELECT DISTINCT name_norm FROM aliases
   WHERE alias_norm LIKE '%' || :q_like || '%' ESCAPE '\\'
      OR (LENGTH(alias_norm) >= 2 AND INSTR(:q_raw, alias_norm) > 0)
)
"""


def _escape_like(s: str) -> str:
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


@router.get("/performers", response_model=PerformerListOut)
def list_performers(
    request: Request,
    q: str = "",
    include_missing: bool = True,
    source_id: int | None = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=PAGE_SIZE_DEFAULT, ge=1),
) -> dict:
    if page_size > PAGE_SIZE_MAX:
        raise HTTPException(status_code=422, detail=f"page_size 上限 {PAGE_SIZE_MAX}")
    conn = _conn(request)
    with _lock(request):
        if source_id is not None:
            src = conn.execute("SELECT id FROM sources WHERE id=?", (source_id,)).fetchone()
            if src is None:
                raise HTTPException(status_code=422, detail="source_id 无效")

        where = ["1=1"]
        params: dict = {}
        prefix = ""
        if q.strip():
            prefix = _MATCH_SQL
            nq = normalize(q)
            params["q_like"] = _escape_like(nq)  # escaped for LIKE only
            params["q_raw"] = nq  # raw for INSTR (reverse containment)
            where.append("p.name_norm IN (SELECT name_norm FROM matched_groups)")
        if not include_missing:
            where.append("p.is_missing = 0")
        if source_id is not None:
            where.append("p.source_id = :source_id")
            params["source_id"] = source_id
        cond = " AND ".join(where)

        stats = conn.execute(
            f"{prefix}SELECT COUNT(*) AS total, COUNT(DISTINCT p.source_id) AS source_count"
            f" FROM performers p WHERE {cond}",
            params,
        ).fetchone()
        # Global (unfiltered) missing count: the purge endpoint deletes ALL
        # missing rows, so its confirmation must not be scoped by q/filters.
        missing_total = conn.execute(
            "SELECT COUNT(*) AS c FROM performers WHERE is_missing=1"
        ).fetchone()["c"]

        rows = conn.execute(
            f"{prefix}SELECT p.*, s.label AS source_label FROM performers p"
            f" JOIN sources s ON s.id = p.source_id"
            f" WHERE {cond} ORDER BY p.name_norm ASC, p.id ASC"
            f" LIMIT :limit OFFSET :offset",
            {**params, "limit": page_size, "offset": (page - 1) * page_size},
        ).fetchall()

        # D5 read model: one alias fetch for all groups on this page.
        norms = sorted({r["name_norm"] for r in rows})
        alias_map: dict[str, list[dict]] = {n: [] for n in norms}
        if norms:
            ph = ",".join("?" for _ in norms)
            for a in conn.execute(
                f"SELECT id, name_norm, alias FROM aliases WHERE name_norm IN ({ph})"
                " ORDER BY id",
                norms,
            ).fetchall():
                alias_map[a["name_norm"]].append({"id": a["id"], "alias": a["alias"]})

    return {
        "items": [
            {
                "id": r["id"],
                "name": r["name"],
                "source_id": r["source_id"],
                "source_label": r["source_label"],
                "unc_path": r["unc_path"],
                "is_missing": bool(r["is_missing"]),
                "has_thumb": r["thumb"] is not None,
                "aliases": alias_map.get(r["name_norm"], []),
            }
            for r in rows
        ],
        "total": stats["total"],
        "page": page,
        "page_size": page_size,
        "source_count": stats["source_count"],
        "missing_total": missing_total,
    }


# ---------------------------------------------------------------------- thumb


@router.get("/performers/{performer_id}/thumb")
def get_thumb(request: Request, performer_id: str) -> Response:
    with _lock(request):
        row = _conn(request).execute(
            "SELECT thumb, thumb_mtime FROM performers WHERE id=?", (performer_id,)
        ).fetchone()
    if row is None or row["thumb"] is None:
        raise HTTPException(status_code=404, detail="无缩略图")
    return Response(
        content=row["thumb"],
        media_type="image/jpeg",
        headers={
            "Cache-Control": "private, max-age=86400",
            "ETag": f'"{row["thumb_mtime"]}"',
        },
    )


# -------------------------------------------------------------------- aliases


class AliasIn(BaseModel):
    alias: str


@router.post("/performers/{performer_id}/aliases", status_code=201, response_model=AliasOut)
def add_alias(request: Request, performer_id: str, body: AliasIn) -> dict:
    alias = body.alias.strip()
    if not alias:
        raise HTTPException(status_code=422, detail="别名不能为空")
    conn = _conn(request)
    with _lock(request):
        row = conn.execute(
            "SELECT name_norm FROM performers WHERE id=?", (performer_id,)
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="表演者不存在")
        group = row["name_norm"]
        alias_norm = normalize(alias)
        if alias_norm == group:
            raise HTTPException(status_code=409, detail="别名与名字相同")
        try:
            cur = conn.execute(
                "INSERT INTO aliases(name_norm, alias, alias_norm) VALUES (?,?,?)",
                (group, alias, alias_norm),
            )
        except sqlite3.IntegrityError:
            conn.rollback()
            raise HTTPException(status_code=409, detail="同名组内该别名已存在") from None
        conn.commit()
        return {"id": cur.lastrowid, "alias": alias}


@router.delete("/aliases/{alias_id}", status_code=204)
def delete_alias(request: Request, alias_id: int) -> None:
    conn = _conn(request)
    with _lock(request):
        cur = conn.execute("DELETE FROM aliases WHERE id=?", (alias_id,))
        if cur.rowcount == 0:
            conn.rollback()  # 0-row DML still opened an implicit transaction
            raise HTTPException(status_code=404, detail="别名不存在")
        conn.commit()


# ------------------------------------------------------- delete/purge/settings


@router.delete("/performers/{performer_id}", status_code=204)
def delete_performer(request: Request, performer_id: str) -> None:
    """Single-record delete (UI offers it on missing rows only). D5: group
    aliases are intentionally untouched."""
    conn = _conn(request)
    with _lock(request):
        cur = conn.execute("DELETE FROM performers WHERE id=?", (performer_id,))
        if cur.rowcount == 0:
            conn.rollback()  # 0-row DML still opened an implicit transaction
            raise HTTPException(status_code=404, detail="表演者不存在")
        conn.commit()


@router.post("/performers/purge-missing", response_model=PurgeOut)
def purge_missing(request: Request) -> dict:
    conn = _conn(request)
    with _lock(request):
        cur = conn.execute("DELETE FROM performers WHERE is_missing=1")
        conn.commit()
        return {"deleted": cur.rowcount}


@router.get("/settings", response_model=SettingsOut)
def get_settings(request: Request) -> dict:
    with _lock(request):
        row = _conn(request).execute(
            "SELECT value FROM settings WHERE key='last_local_dir'"
        ).fetchone()
    return {
        "last_local_dir": row["value"] if row else None,
        "db_path": request.app.state.db_path,
    }


class SettingsIn(BaseModel):
    last_local_dir: str | None = None


@router.put("/settings", response_model=SettingsOut)
def put_settings(request: Request, body: SettingsIn) -> dict:
    conn = _conn(request)
    with _lock(request):
        conn.execute(
            "INSERT INTO settings(key, value) VALUES ('last_local_dir', ?)"
            " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (body.last_local_dir,),
        )
        conn.commit()
    return {
        "last_local_dir": body.last_local_dir,
        "db_path": request.app.state.db_path,
    }


# ------------------------------------------------------------- open / local


@router.post("/performers/{performer_id}/open", status_code=204)
def open_performer(request: Request, performer_id: str) -> None:
    conn = _conn(request)
    with _lock(request):
        row = conn.execute(
            "SELECT unc_path, is_missing FROM performers WHERE id=?", (performer_id,)
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="表演者不存在")
    # Reachability check OUTSIDE the lock: an unreachable UNC path can block
    # for the whole network timeout and must not freeze every other request.
    reachable = os.path.isdir(row["unc_path"])
    if reachable and row["is_missing"]:
        # The share was just offline at last scan — folder is back, un-flag
        # it. Guard WHERE is_missing=1: the probe ran outside the lock and a
        # concurrent scan may already have updated the row (stale read).
        with _lock(request):
            conn.execute(
                "UPDATE performers SET is_missing=0 WHERE id=? AND is_missing=1",
                (performer_id,),
            )
            conn.commit()
    if not reachable:
        with _lock(request):
            # Re-check the row still exists before flagging (it may have been
            # deleted while we probed the share).
            still = conn.execute(
                "SELECT 1 FROM performers WHERE id=?", (performer_id,)
            ).fetchone()
            if still is not None:
                conn.execute(
                    "UPDATE performers SET is_missing=1 WHERE id=?", (performer_id,)
                )
                conn.commit()
        raise HTTPException(status_code=404, detail="文件夹已不存在, 已标记失效")
    open_in_explorer(row["unc_path"])


@router.get("/local/subdirs", response_model=SubdirsOut)
def local_subdirs(path: str) -> dict:
    try:
        names = list_subdirs(path)
    except SourceUnreachable:
        raise HTTPException(status_code=422, detail="目录不存在或不可访问") from None
    return {"path": path, "subdirs": names}


class OpenPairIn(BaseModel):
    local_path: str
    performer_id: str


@router.post("/open-pair", status_code=204)
def open_pair(request: Request, body: OpenPairIn) -> None:
    if not os.path.isdir(body.local_path):
        raise HTTPException(status_code=422, detail="本地目录不存在 — 请重新选择")
    conn = _conn(request)
    with _lock(request):
        row = conn.execute(
            "SELECT unc_path, is_missing FROM performers WHERE id=?", (body.performer_id,)
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="表演者不存在")
        if row["is_missing"]:
            raise HTTPException(status_code=409, detail="该记录已失效, 无法双开")
    open_in_explorer(body.local_path)
    open_in_explorer(row["unc_path"])
