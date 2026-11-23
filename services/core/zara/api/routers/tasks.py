"""Task CRUD endpoints (07-API.md §3)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request, Response, status

from zara.api.deps import (
    DbDep,
    PrincipalDep,
    SettingsDep,
    UserRateLimit,
    client_ip,
    parse_if_match,
)
from zara.api.pagination import MAX_PAGE_SIZE
from zara.schemas import Page, TaskCompleteIn, TaskCreate, TaskOut, TaskUpdate
from zara.services import tasks as service

router = APIRouter(prefix="/tasks", tags=["tasks"])

_Writes = UserRateLimit("tasks-write", limit=120)


@router.get("", response_model=Page[TaskOut], summary="List tasks")
async def list_tasks(
    principal: PrincipalDep,
    db: DbDep,
    status_filter: Annotated[str | None, Query(alias="status")] = None,
    due_before: datetime | None = None,
    due_after: datetime | None = None,
    project_id: uuid.UUID | None = None,
    goal_id: uuid.UUID | None = None,
    q: str | None = None,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = 50,
    cursor: str | None = None,
) -> Page[TaskOut]:
    rows, next_cursor = await service.list_tasks(
        db,
        principal,
        status=status_filter,
        due_before=due_before,
        due_after=due_after,
        project_id=project_id,
        goal_id=goal_id,
        q=q,
        limit=limit,
        cursor=cursor,
    )
    return Page(items=[TaskOut.model_validate(row) for row in rows], next_cursor=next_cursor)


@router.post(
    "",
    response_model=TaskOut,
    status_code=status.HTTP_201_CREATED,
    summary="Create a task",
    dependencies=[Depends(_Writes)],
)
async def create_task(
    payload: TaskCreate,
    principal: PrincipalDep,
    db: DbDep,
    settings: SettingsDep,
    request: Request,
) -> TaskOut:
    task = await service.create_task(
        db,
        principal,
        payload.model_dump(exclude_unset=True),
        ip=client_ip(request, settings),
    )
    return TaskOut.model_validate(task)


@router.get("/{task_id}", response_model=TaskOut, summary="Get a task")
async def get_task(task_id: uuid.UUID, principal: PrincipalDep, db: DbDep) -> TaskOut:
    return TaskOut.model_validate(await service.get_task(db, principal, task_id))


@router.patch(
    "/{task_id}",
    response_model=TaskOut,
    summary="Update a task",
    dependencies=[Depends(_Writes)],
)
async def update_task(
    task_id: uuid.UUID,
    payload: TaskUpdate,
    request: Request,
    principal: PrincipalDep,
    db: DbDep,
    settings: SettingsDep,
) -> TaskOut:
    task = await service.update_task(
        db,
        principal,
        task_id,
        payload.model_dump(exclude_unset=True),
        if_match_version=parse_if_match(request),
        ip=client_ip(request, settings),
    )
    return TaskOut.model_validate(task)


@router.delete(
    "/{task_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a task",
    dependencies=[Depends(_Writes)],
)
async def delete_task(
    task_id: uuid.UUID,
    principal: PrincipalDep,
    db: DbDep,
    settings: SettingsDep,
    request: Request,
) -> Response:
    await service.delete_task(db, principal, task_id, ip=client_ip(request, settings))
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/{task_id}/complete",
    response_model=TaskOut,
    summary="Complete a task",
    dependencies=[Depends(_Writes)],
)
async def complete_task(
    task_id: uuid.UUID,
    _payload: TaskCompleteIn,
    principal: PrincipalDep,
    db: DbDep,
    settings: SettingsDep,
    request: Request,
) -> TaskOut:
    task = await service.complete_task(db, principal, task_id, ip=client_ip(request, settings))
    return TaskOut.model_validate(task)
