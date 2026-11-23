"""Activity and audit-trail reads."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from zara.api.pagination import paginate
from zara.auth.principal import Principal
from zara.models import AuditLog
from zara.services.audit import verify_chain


async def list_audit(
    db: AsyncSession,
    principal: Principal,
    *,
    action: str | None = None,
    resource_type: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    limit: int = 50,
    cursor: str | None = None,
) -> tuple[list[AuditLog], str | None]:
    stmt = select(AuditLog).where(AuditLog.tenant_id == principal.tenant_id)
    if action:
        stmt = stmt.where(AuditLog.action == action)
    if resource_type:
        stmt = stmt.where(AuditLog.resource_type == resource_type)
    if since:
        stmt = stmt.where(AuditLog.created_at >= since)
    if until:
        stmt = stmt.where(AuditLog.created_at <= until)
    return await paginate(db, stmt, limit=limit, cursor=cursor, model=AuditLog)


def _activity_type(action: str) -> str:
    prefix = action.split(".", 1)[0]
    return {
        "run": "run",
        "tool": "tool",
        "approval": "approval",
        "memory": "memory",
        "integration": "integration",
        "auth": "security",
        "session": "security",
    }.get(prefix, "system")


def to_activity(row: AuditLog) -> dict[str, Any]:
    return {
        "id": row.id,
        "at": row.created_at,
        "type": _activity_type(row.action),
        "action": row.action,
        "summary": row.action.replace(".", " ").replace("_", " ").capitalize(),
        "resource_type": row.resource_type,
        "resource_id": row.resource_id,
        "run_id": row.run_id,
        "actor": row.actor_type,
    }


async def read_activity(
    db: AsyncSession,
    principal: Principal,
    *,
    type: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    limit: int = 50,
    cursor: str | None = None,
) -> tuple[list[dict[str, Any]], str | None]:
    rows, next_cursor = await list_audit(
        db,
        principal,
        since=since,
        until=until,
        limit=limit,
        cursor=cursor,
    )
    items = [to_activity(row) for row in rows]
    if type:
        items = [item for item in items if item["type"] == type]
    return items, next_cursor


async def check_chain(db: AsyncSession, principal: Principal) -> tuple[bool, list[uuid.UUID]]:
    return await verify_chain(db, principal.tenant_id)
