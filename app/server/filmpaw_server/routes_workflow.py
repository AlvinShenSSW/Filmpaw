"""Workflow archive routes for Taskpaw (F02-P1): target proof, approved-package copy, lookup by operation key.

Taskpaw finds the port and session token in ``workflow-endpoint.json`` next to the library DB.
"""

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field

router = APIRouter(prefix="/api/workflow/archive", tags=["workflow"])


class FileIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str = Field(min_length=1, max_length=1024)
    size: int = Field(ge=0)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class TargetIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    target_path: str = Field(min_length=1, max_length=32767)
    directory_identity: list[int | str] = Field(min_length=1, max_length=4)


class ArchiveIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    operation_id: str = Field(min_length=1, max_length=128)
    package_dir: str = Field(min_length=1, max_length=32767)
    performer_id: str = Field(min_length=1, max_length=128)
    expected_target: TargetIn
    files: list[FileIn] = Field(min_length=1, max_length=4096)


@router.get("/target")
def target(
    request: Request,
    performer_id: str = Query(min_length=1, max_length=128),
    film_dir: str = Query(min_length=1, max_length=255),
) -> dict:
    return request.app.state.transfers.workflow_target(performer_id, film_dir)


@router.post("", status_code=202)
def archive(body: ArchiveIn, request: Request) -> dict:
    return request.app.state.transfers.create_workflow_task(
        body.operation_id,
        body.package_dir,
        body.performer_id,
        body.expected_target.model_dump(),
        [f.model_dump() for f in body.files],
    )


@router.get("/{operation_id}")
def lookup(operation_id: str, request: Request) -> dict:
    task = request.app.state.transfers.get_workflow_task(operation_id)
    if task is None:
        raise HTTPException(status_code=404, detail="归档操作不存在")
    return task
