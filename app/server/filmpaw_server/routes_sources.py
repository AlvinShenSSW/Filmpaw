"""Sources CRUD + scan endpoints per design §6."""

import os
import sqlite3
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from filmpaw_server.scan import SourceUnreachable, scan_source

router = APIRouter(prefix="/api")


def _conn(request: Request) -> sqlite3.Connection:
    return request.app.state.db


def _normalize_unc(p: str) -> str:
    p = p.strip().replace("/", "\\")
    return p if p.endswith("\\") else p + "\\"


class SourceIn(BaseModel):
    unc_path: str
    label: str | None = None


@router.get("/sources")
def list_sources(request: Request) -> list[dict]:
    conn = _conn(request)
    rows = conn.execute(
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


@router.post("/sources", status_code=201)
def add_source(request: Request, body: SourceIn) -> dict:
    conn = _conn(request)
    unc = _normalize_unc(body.unc_path)
    if not os.path.isdir(unc):
        raise HTTPException(status_code=422, detail=f"路径不可达或不是目录: {unc}")
    label = body.label or Path(unc.rstrip("\\")).name
    try:
        cur = conn.execute(
            "INSERT INTO sources(unc_path, label) VALUES (?, ?)", (unc, label)
        )
    except sqlite3.IntegrityError:
        raise HTTPException(status_code=409, detail="该源已存在") from None
    conn.commit()
    return {"id": cur.lastrowid, "unc_path": unc, "label": label}


@router.delete("/sources/{source_id}", status_code=204)
def delete_source(request: Request, source_id: int) -> None:
    conn = _conn(request)
    cur = conn.execute("DELETE FROM sources WHERE id=?", (source_id,))
    if cur.rowcount == 0:
        raise HTTPException(status_code=404, detail="源不存在")
    conn.commit()


@router.post("/sources/{source_id}/scan")
def scan_one(request: Request, source_id: int) -> dict:
    conn = _conn(request)
    try:
        result = scan_source(conn, source_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="源不存在") from None
    except SourceUnreachable:
        raise HTTPException(status_code=503, detail="源不可达 — 已跳过, 记录未变动") from None
    return {"added": result.added, "refreshed": result.refreshed, "missing": result.missing}


@router.post("/scan-all")
def scan_all(request: Request) -> list[dict]:
    conn = _conn(request)
    out: list[dict] = []
    for row in conn.execute("SELECT id FROM sources ORDER BY id").fetchall():
        sid = row["id"]
        try:
            r = scan_source(conn, sid)
            out.append(
                {"source_id": sid, "ok": True, "added": r.added, "refreshed": r.refreshed, "missing": r.missing}
            )
        except SourceUnreachable:
            out.append({"source_id": sid, "ok": False, "error": "源不可达"})
    return out
