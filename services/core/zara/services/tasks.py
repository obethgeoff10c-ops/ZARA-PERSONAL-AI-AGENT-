"""Task CRUD with optimistic concurrency and ownership validation."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from zara.api.pagination import paginate
from zara.auth.principal import Principal
from zara.errors import conflict, not_found, precondition_failed, validation_failed
from zara.models import Contact, Goal, Project, Task
from zara.services.audit import record_event


async def _assert_owned_references(
    db: AsyncSession, principal: Principal, values: dict[str, Any]
) -> None:
    checks = (("project_id", Project), ("goal_id", Goal), ("contact_id", Contact))
    for field, model in checks:
        reference = values.get(field)
        if reference is None:
            continue
        found = await db.scalar(
            select(model.id).where(
                model.id == reference,
                model.tenant_id == principal.tenant_id,
                model.user_id == principal.user_id,
            )
        )
        if found is None:
            raise validation_failed(
                f"The referenced {field.replace('_id', '')} does not exist.",
                errors=[{"field": field, "message": "Unknown or inaccessible reference"}],
            )


async def list_tasks(
    db: AsyncSession,
    principal: Principal,
    *,
    status: str | None = None,
    due_before: datetime | None = None,
    due_after: datetime | None = None,
    project_id: uuid.UUID | None = None,
    goal_id: uuid.UUID | None = None,
    q: str | None = None,
    limit: int = 50,
    cursor: str | None = None,
) -> tuple[list[Task], str | None]:
    stmt = select(Task).where(
        Task.tenant_id == principal.tenant_id,
        Task.user_id == principal.user_id,
    )
    if status:
        stmt = stmt.where(Task.status == status)
    if due_before:
        stmt = stmt.where(Task.due_at <= due_before)
    if due_after:
        stmt = stmt.where(Task.due_at >= due_after)
    if project_id:
        stmt = stmt.where(Task.project_id == project_id)
    if goal_id:
        stmt = stmt.where(Task.goal_id == goal_id)
    if q:
        stmt = stmt.where(Task.title.ilike(f"%{q}%"))
    return await paginate(db, stmt, limit=limit, cursor=cursor, model=Task)


async def get_task(db: AsyncSession, principal: Principal, task_id: uuid.UUID) -> Task:
    task = (
        await db.execute(
            select(Task).where(
                Task.id == task_id,
                Task.tenant_id == principal.tenant_id,
                Task.user_id == principal.user_id,
            )
        )
    ).scalar_one_or_none()
    if task is None:
        raise not_found("Task not found.")
    return task


async def create_task(
    db: AsyncSession,
    principal: Principal,
    values: dict[str, Any],
    *,
    ip: str | None = None,
) -> Task:
    await _assert_owned_references(db, principal, values)
    task = Task(
        tenant_id=principal.tenant_id,
        user_id=principal.user_id,
        source=values.get("source", "user"),
        **values,
    )
    db.add(task)
    await db.flush()
    await record_event(
        db,
        principal,
        action="task.created",
        resource_type="task",
        resource_id=str(task.id),
        metadata={"status": task.status},
        ip=ip,
    )
    return task


async def update_task(
    db: AsyncSession,
    principal: Principal,
    task_id: uuid.UUID,
    changes: dict[str, Any],
    *,
    if_match_version: int | None,
    ip: str | None = None,
) -> Task:
    task = await get_task(db, principal, task_id)
    if if_match_version is not None and task.version != if_match_version:
        raise precondition_failed("The task was modified by another request; reload and retry.")
    await _assert_owned_references(db, principal, changes)
    for field, value in changes.items():
        setattr(task, field, value)
    if changes.get("status") == "done" and task.completed_at is None:
        task.completed_at = datetime.now(UTC)
    elif changes.get("status") not in (None, "done"):
        task.completed_at = None
    await db.flush()
    await record_event(
        db,
        principal,
        action="task.updated",
        resource_type="task",
        resource_id=str(task.id),
        metadata={"fields": sorted(changes), "version": task.version},
        ip=ip,
    )
    return task


async def complete_task(
    db: AsyncSession,
    principal: Principal,
    task_id: uuid.UUID,
    *,
    ip: str | None = None,
) -> Task:
    task = await get_task(db, principal, task_id)
    if task.status == "done":
        raise conflict("This task is already complete.")
    task.status = "done"
    task.completed_at = datetime.now(UTC)
    await db.flush()
    await record_event(
        db,
        principal,
        action="task.completed",
        resource_type="task",
        resource_id=str(task.id),
        ip=ip,
    )
    return task


async def delete_task(
    db: AsyncSession,
    principal: Principal,
    task_id: uuid.UUID,
    *,
    ip: str | None = None,
) -> None:
    task = await get_task(db, principal, task_id)
    await record_event(
        db,
        principal,
        action="task.deleted",
        resource_type="task",
        resource_id=str(task.id),
        ip=ip,
    )
    await db.delete(task)
    await db.flush()
