"""Archive routes accept server-issued context IDs, never write paths."""

from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

router = APIRouter(prefix="/api/archive", tags=["archive"])


class ContextIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    local_dir: str = Field(min_length=1, max_length=32767)
    subdir: str = Field(min_length=1, max_length=255)
    performer_id: str = Field(min_length=1, max_length=128)


class MovieOut(BaseModel):
    name: str
    size_bytes: int | None = None
    files: int | None = None
    error: str | None = None


class BindingOut(BaseModel):
    local_dir: str
    subdir: str
    performer_id: str
    performer_name: str
    source_label: str | None = None
    has_thumb: bool = False
    source_path: str
    target_path: str


class ContextOut(BindingOut):
    id: str
    source_items: list[MovieOut]
    target_items: list[MovieOut]
    source_error: str | None = None
    target_error: str | None = None


class TransferIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    context_id: str = Field(min_length=1, max_length=128)
    names: list[str] = Field(min_length=1, max_length=1000)
    request_id: str = Field(min_length=1, max_length=128)


class TransferItemOut(BaseModel):
    name: str
    status: str
    size_bytes: int = 0
    progress_bytes: int = 0
    message: str | None = None


class TaskOut(BindingOut):
    id: str
    request_id: str
    context_id: str
    status: Literal["queued", "running", "completed", "stopped", "failed", "needs_review"]
    phase: str
    current_name: str | None = None
    progress_bytes: int = 0
    total_bytes: int = 0
    verified_bytes: int = 0
    completed_count: int = 0
    total_count: int
    conflicts: list[str] = Field(default_factory=list)
    message: str | None = None
    stop_requested: bool = False
    created_at: str
    updated_at: str
    items: list[TransferItemOut]


class ExitOut(BaseModel):
    safe: bool
    task_id: str | None = None


@router.post("/contexts", status_code=201, response_model=ContextOut)
def create_context(request: Request, body: ContextIn):
    return request.app.state.transfers.create_context(
        body.local_dir, body.subdir, body.performer_id,
    )


@router.post("/transfers", status_code=202, response_model=TaskOut)
def create_transfer(request: Request, body: TransferIn):
    return request.app.state.transfers.submit(body.context_id, body.names, body.request_id)


@router.get("/transfers/latest", response_model=TaskOut | None)
def latest_transfer(request: Request):
    return request.app.state.transfers.get_task()


@router.get("/transfers/{task_id}", response_model=TaskOut)
def get_transfer(request: Request, task_id: str):
    result = request.app.state.transfers.get_task(task_id)
    if result is None:
        raise HTTPException(404, "归档任务不存在")
    return result


@router.post("/transfers/{task_id}/stop", response_model=TaskOut)
def stop_transfer(request: Request, task_id: str):
    return request.app.state.transfers.stop(task_id)


@router.post("/prepare-exit", response_model=ExitOut)
def prepare_exit(request: Request):
    return request.app.state.transfers.prepare_exit()
