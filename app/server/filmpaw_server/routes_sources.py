"""Sources CRUD + scan endpoints per design §6.

All handlers serialize on app.state.db_lock: FastAPI sync endpoints run on
worker threads sharing one sqlite connection, and overlapping requests
(scan vs scan-all) must not interleave on it.
"""

import os
import sqlite3
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from filmpaw_server.scan import ScanResult, SourceUnreachable, scan_source


def _result_dict(r: ScanResult) -> dict:
    """Single source of truth for the scan-summary JSON shape."""
    return {"added": r.added, "refreshed": r.refreshed, "missing": r.missing}

router = APIRouter(prefix="/api")


def _conn(request: Request) -> sqlite3.Connection:
    return request.app.state.db


def _lock(request: Request):
    return request.app.state.db_lock


def _normalize_unc(p: str) -> str:
    p = p.strip().replace("/", "\\")
    return p if p.endswith("\\") else p + "\\"


class SourceIn(BaseModel):
    unc_path: str
    label: str | None = None


class SourceOut(BaseModel):
    id: int
    unc_path: str
    label: str | None
    last_scan_at: str | None
    performer_count: int
    reachable: bool


class SourceCreated(BaseModel):
    id: int
    unc_path: str
    label: str


class ScanSummary(BaseModel):
    added: int
    refreshed: int
    missing: int


class ScanAllItem(BaseModel):
    source_id: int
    ok: bool
    added: int | None = None
    refreshed: int | None = None
    missing: int | None = None
    error: str | None = None


@router.get("/sources", response_model=list[SourceOut])
def list_sources(request: Request) -> list[dict]:
    with _lock(request):
        rows = _conn(request).execute(
            "SELECT s.*, (SELECT COUNT(*) FROM performers p WHERE p.source_id = s.id)"
            " AS performer_count FROM sources s ORDER BY s.id"
        ).fetchall()
    return [
        {
            "id": r["id"],
            "unc_path": r["unc_path"],
            "label": r["label"],
            "last_scan_at": r["last_scan_at"],
            "performer_count": r["performer_count"],
            "reachable": os.path.isdir(r["unc_path"]),
        }
        for r in rows
    ]


@router.post("/sources", status_code=201, response_model=SourceCreated)
def add_source(request: Request, body: SourceIn) -> dict:
    unc = _normalize_unc(body.unc_path)
    if not os.path.isdir(unc):
        raise HTTPException(status_code=422, detail=f"路径不可达或不是目录: {unc}")
    label = body.label or Path(unc.rstrip("\\")).name
    with _lock(request):
        conn = _conn(request)
        try:
            cur = conn.execute(
                "INSERT INTO sources(unc_path, label) VALUES (?, ?)", (unc, label)
            )
        except sqlite3.IntegrityError:
            # Keep the shared-connection invariant: never release the lock
            # with an open transaction (statement rolled back, txn not).
            conn.rollback()
            raise HTTPException(status_code=409, detail="该源已存在") from None
        conn.commit()
        return {"id": cur.lastrowid, "unc_path": unc, "label": label}


@router.delete("/sources/{source_id}", status_code=204)
def delete_source(request: Request, source_id: int) -> None:
    with _lock(request):
        conn = _conn(request)
        cur = conn.execute("DELETE FROM sources WHERE id=?", (source_id,))
        if cur.rowcount == 0:
            conn.rollback()  # 0-row DML still opened an implicit transaction
            raise HTTPException(status_code=404, detail="源不存在")
        conn.commit()


@router.post("/sources/{source_id}/scan", response_model=ScanSummary)
def scan_one(request: Request, source_id: int) -> dict:
    with _lock(request):
        try:
            result = scan_source(_conn(request), source_id)
        except KeyError:
            raise HTTPException(status_code=404, detail="源不存在") from None
        except SourceUnreachable:
            raise HTTPException(
                status_code=503, detail="源不可达 — 已跳过, 记录未变动"
            ) from None
    return _result_dict(result)


@router.post("/scan-all", response_model=list[ScanAllItem])
def scan_all(request: Request) -> list[dict]:
    out: list[dict] = []
    conn = _conn(request)
    with _lock(request):
        ids = [r["id"] for r in conn.execute("SELECT id FROM sources ORDER BY id").fetchall()]
    # Acquire the lock per source (not across the whole sweep) so reads —
    # e.g. the UI refreshing GET /api/sources — stay responsive between
    # sources during a long multi-NAS scan.
    for sid in ids:
        with _lock(request):
            try:
                r = scan_source(conn, sid)
                out.append({"source_id": sid, "ok": True, **_result_dict(r)})
            except KeyError:
                out.append({"source_id": sid, "ok": False, "error": "源已被删除"})
            except SourceUnreachable:
                out.append({"source_id": sid, "ok": False, "error": "源不可达"})
    return out
