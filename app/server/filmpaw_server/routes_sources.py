"""Source CRUD and bounded scans; SQLite locks never surround NAS I/O."""

import asyncio
import sqlite3
import time
from pathlib import Path
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from filmpaw_server.workflow_models import Health, ProbeDraft, ScanFinish, WorkflowIndexError


def _result_dict(r: ScanFinish) -> dict:
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
    unc_path: str = Field(min_length=1, max_length=32767)
    label: str | None = Field(default=None, max_length=2048)


class SourceOut(BaseModel):
    id: int
    unc_path: str
    label: str | None
    last_scan_at: str | None
    performer_count: int
    reachable: bool
    health_state: Health = "never_scanned"
    observed_at: str | None = None
    last_success_at: str | None = None


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
    error_code: str | None = None


@router.get("/sources", response_model=list[SourceOut])
def list_sources(request: Request) -> list[dict]:
    with _lock(request):
        rows = _conn(request).execute(
            "SELECT s.*, (SELECT COUNT(*) FROM performers p WHERE p.source_id = s.id)"
            " AS performer_count FROM sources s ORDER BY s.id"
        ).fetchall()
        index = request.app.state.workflow_index
        health = index.source_health(tuple(r["id"] for r in rows))
        now = index.clock()
    out = []
    for r in rows:
        observation = health.get(r["id"])
        probe_fresh = bool(observation and observation.health == "never_scanned"
                           and observation.probe_health == "fresh" and observation.probe_observed_at
                           and 0 <= (now - observation.probe_observed_at).total_seconds() <= index.profile.freshness_seconds)
        observed_at = (observation.observed_at or observation.probe_observed_at) if observation else None
        out.append({
            "id": r["id"],
            "unc_path": r["unc_path"],
            "label": r["label"],
            "last_scan_at": r["last_scan_at"],
            "performer_count": r["performer_count"],
            "reachable": bool(observation and (observation.health == "fresh" or probe_fresh)),
            "health_state": observation.health if observation else "never_scanned",
            "observed_at": observed_at.isoformat() if observed_at else None,
            "last_success_at": observation.last_success_at.isoformat() if observation and observation.last_success_at else None,
        })
    return out


@router.post("/sources", status_code=201, response_model=SourceCreated)
async def add_source(request: Request, body: SourceIn) -> dict:
    unc = _normalize_unc(body.unc_path)
    label = body.label or Path(unc.rstrip("\\")).name
    with _lock(request):
        if _conn(request).execute("SELECT id FROM sources WHERE unc_path=?", (unc,)).fetchone():
            raise HTTPException(status_code=409, detail="该源已存在")
    ticket = request.app.state.scans.request_probe(
        ProbeDraft(private_root_path=unc, label=label), request_id=str(uuid4()))
    profile = request.app.state.workflow_index.profile
    result = await _wait(request, ticket, time.monotonic() + profile.probe_seconds + profile.cleanup_seconds)
    if not result.published or result.source_id is None:
        _raise_scan_failure(result, adding=True)
    return {"id": result.source_id, "unc_path": unc, "label": label}


@router.delete("/sources/{source_id}", status_code=204)
def delete_source(request: Request, source_id: int) -> None:
    with _lock(request), _conn(request):
        conn = _conn(request)
        if conn.execute("SELECT id FROM sources WHERE id=?", (source_id,)).fetchone() is None:
            raise HTTPException(status_code=404, detail="源不存在")
        request.app.state.workflow_index.tombstone_source_tx(source_id)
        conn.execute("DELETE FROM sources WHERE id=?", (source_id,))


async def _wait(request, ticket, deadline):
    try:
        result = await request.app.state.scans.wait(ticket, deadline=deadline)
        if not result.published:
            reason = "timeout" if result.status in {"queued", "starting", "running"} else "cancelled"
            # Cancellation and a racing complete snapshot serialize in SQLite.
            # If completion already won, return its fact; otherwise late results
            # must not publish after this request reports failure.
            result = request.app.state.workflow_index.cancel_publication(ticket, reason=reason)
            if not result.published and result.error_code is None:
                result = result.model_copy(update={"error_code": reason})
        return result
    except asyncio.CancelledError:
        request.app.state.workflow_index.cancel_publication(ticket, reason="cancelled")
        raise


def _raise_scan_failure(result, *, adding=False):
    code = "unknown_owner" if result.owner_state == "unknown_owner" else result.error_code or "partial"
    raise WorkflowIndexError(code, 422 if adding and code == "not_directory" else 503)


@router.post("/sources/{source_id}/scan", response_model=ScanSummary)
async def scan_one(request: Request, source_id: int) -> dict:
    index = request.app.state.workflow_index
    root_id = index.root_for_source(source_id)
    ticket = request.app.state.scans.request(root_id, request_id=str(uuid4()))
    result = await _wait(request, ticket, time.monotonic() + index.profile.scan_seconds + index.profile.cleanup_seconds)
    if not result.published:
        _raise_scan_failure(result)
    return _result_dict(result)


@router.post("/scan-all", response_model=list[ScanAllItem])
async def scan_all(request: Request) -> list[dict]:
    conn = _conn(request)
    with _lock(request):
        ids = [r["id"] for r in conn.execute("SELECT id FROM sources ORDER BY id").fetchall()]
    index = request.app.state.workflow_index
    deadline = time.monotonic() + index.profile.scan_seconds + index.profile.cleanup_seconds

    async def one(sid):
        try:
            ticket = request.app.state.scans.request(index.root_for_source(sid), request_id=str(uuid4()))
            result = await _wait(request, ticket, deadline)
            if not result.published:
                _raise_scan_failure(result)
            return {"source_id": sid, "ok": True, **_result_dict(result)}
        except WorkflowIndexError as exc:
            return {"source_id": sid, "ok": False, "error_code": exc.code, "error": "扫描未完成，原索引已保留"}

    return await asyncio.gather(*(one(sid) for sid in ids))
