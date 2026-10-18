"""Manual empty-actor cleanup is authorized solely by the selected batch."""

from typing import Literal

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict, Field

router = APIRouter(prefix="/api/archive", tags=["archive-cleanup"])


class CleanupContextIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    batch_path: str = Field(min_length=1, max_length=32767)


class CleanupContextOut(BaseModel):
    id: str
    batch_path: str
    batch_revision: str


class CleanupIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    context_id: str = Field(min_length=1, max_length=128)
    request_id: str = Field(min_length=1, max_length=128)


class CleanupItemOut(BaseModel):
    name: str
    status: Literal["deleted", "not_empty", "skipped", "failed"]
    message: str | None = None


class CleanupTaskOut(CleanupContextOut):
    context_id: str
    request_id: str
    status: Literal["queued", "running", "completed", "failed"]
    current_name: str | None = None
    checked_count: int
    total_count: int
    deleted_count: int
    kept_count: int
    skipped_count: int
    failed_count: int
    message: str | None = None
    items: list[CleanupItemOut]


@router.post("/cleanup-contexts", status_code=201, response_model=CleanupContextOut)
def create_cleanup_context(request: Request, body: CleanupContextIn):
    return request.app.state.cleanups.create_context(body.batch_path)


@router.post("/cleanups", status_code=202, response_model=CleanupTaskOut)
def create_cleanup(request: Request, body: CleanupIn):
    return request.app.state.cleanups.submit(body.context_id, body.request_id)


@router.get("/cleanups/latest", response_model=CleanupTaskOut | None)
def latest_cleanup(request: Request):
    return request.app.state.cleanups.get_task()


@router.get("/cleanups/{task_id}", response_model=CleanupTaskOut)
def get_cleanup(request: Request, task_id: str):
    return request.app.state.cleanups.get_task(task_id)
