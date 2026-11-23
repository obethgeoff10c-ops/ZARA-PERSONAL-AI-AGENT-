"""Activity and audit endpoints (07-API.md §10)."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Query

from zara.api.deps import DbDep, PrincipalDep
from zara.api.pagination import MAX_PAGE_SIZE
from zara.schemas import Page
from zara.schemas.activity import ActivityItem, AuditItem, ChainCheckOut
from zara.services import activity as service
from zara.services.permissions import require_permission

router = APIRouter(prefix="/activity", tags=["activity"])


@router.get("", response_model=Page[ActivityItem], summary="Activity feed")
async def activity(
    principal: PrincipalDep,
    db: DbDep,
    type: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = 50,
    cursor: str | None = None,
) -> Page[ActivityItem]:
    items, next_cursor = await service.read_activity(
        db,
        principal,
        type=type,
        since=since,
        until=until,
        limit=limit,
        cursor=cursor,
    )
    return Page(
        items=[ActivityItem.model_validate(item) for item in items], next_cursor=next_cursor
    )


@router.get("/audit", response_model=Page[AuditItem], summary="Audit trail (metadata only)")
async def audit(
    principal: PrincipalDep,
    db: DbDep,
    action: str | None = None,
    resource_type: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = 50,
    cursor: str | None = None,
) -> Page[AuditItem]:
    require_permission(principal, "audit:read")
    rows, next_cursor = await service.list_audit(
        db,
        principal,
        action=action,
        resource_type=resource_type,
        since=since,
        until=until,
        limit=limit,
        cursor=cursor,
    )
    items = [
        AuditItem(
            id=row.id,
            tenant_id=row.tenant_id,
            user_id=row.user_id,
            actor_type=row.actor_type,
            action=row.action,
            resource_type=row.resource_type,
            resource_id=row.resource_id,
            run_id=row.run_id,
            metadata=row.metadata_json,
            trace_id=row.trace_id,
            created_at=row.created_at,
        )
        for row in rows
    ]
    return Page(items=items, next_cursor=next_cursor)


@router.get(
    "/audit/verify",
    response_model=ChainCheckOut,
    summary="Verify the tenant audit hash chain",
)
async def verify(
    principal: PrincipalDep,
    db: DbDep,
) -> ChainCheckOut:
    require_permission(principal, "audit:read")
    valid, broken = await service.check_chain(db, principal)
    return ChainCheckOut(valid=valid, broken_ids=broken)
