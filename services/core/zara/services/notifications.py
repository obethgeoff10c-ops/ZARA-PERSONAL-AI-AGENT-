"""In-app notification reads and acknowledgement."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from zara.api.pagination import paginate
from zara.auth.principal import Principal
from zara.errors import not_found
from zara.models import Notification


def _base(principal: Principal) -> object:
    result = select(Notification).where(
        Notification.tenant_id == principal.tenant_id,
        Notification.user_id == principal.user_id,
    )
    return result


async def list_notifications(
    db: AsyncSession,
    principal: Principal,
    *,
    unread: bool | None = None,
    kind: str | None = None,
    limit: int = 50,
    cursor: str | None = None,
) -> tuple[list[Notification], str | None, int]:
    stmt = select(Notification).where(
        Notification.tenant_id == principal.tenant_id,
        Notification.user_id == principal.user_id,
    )
    if unread:
        stmt = stmt.where(Notification.read_at.is_(None))
    if kind:
        stmt = stmt.where(Notification.kind == kind)
    items, next_cursor = await paginate(db, stmt, limit=limit, cursor=cursor, model=Notification)
    unread_count = int(
        await db.scalar(
            select(func.count(Notification.id)).where(
                Notification.tenant_id == principal.tenant_id,
                Notification.user_id == principal.user_id,
                Notification.read_at.is_(None),
            )
        )
        or 0
    )
    return items, next_cursor, unread_count


async def mark_read(db: AsyncSession, principal: Principal, notification_id: uuid.UUID) -> None:
    notification = (
        await db.execute(
            select(Notification).where(
                Notification.id == notification_id,
                Notification.tenant_id == principal.tenant_id,
                Notification.user_id == principal.user_id,
            )
        )
    ).scalar_one_or_none()
    if notification is None:
        raise not_found("Notification not found.")
    if notification.read_at is None:
        notification.read_at = datetime.now(UTC)
        if notification.status in {None, "queued", "sent", "delivered"}:
            notification.status = "read"
        await db.flush()


async def mark_all_read(db: AsyncSession, principal: Principal) -> int:
    result = await db.execute(
        update(Notification)
        .where(
            Notification.tenant_id == principal.tenant_id,
            Notification.user_id == principal.user_id,
            Notification.read_at.is_(None),
        )
        .values(read_at=datetime.now(UTC), status="read")
    )
    await db.flush()
    return int(getattr(result, "rowcount", 0) or 0)
