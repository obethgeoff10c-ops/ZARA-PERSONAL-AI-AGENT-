"""Notification and notification-stream endpoints (07-API.md §6)."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Header, Query, Request, Response, status
from fastapi.responses import StreamingResponse

from zara.api.deps import DbDep, PrincipalDep, SettingsDep
from zara.api.pagination import MAX_PAGE_SIZE
from zara.api.sse import SSE_HEADERS, notification_stream
from zara.schemas import NotificationPage
from zara.services import notifications as service

router = APIRouter(prefix="/notifications", tags=["notifications"])


@router.get("", response_model=NotificationPage, summary="List notifications")
async def list_notifications(
    principal: PrincipalDep,
    db: DbDep,
    unread: bool | None = None,
    kind: str | None = None,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = 50,
    cursor: str | None = None,
) -> NotificationPage:
    rows, next_cursor, unread_count = await service.list_notifications(
        db, principal, unread=unread, kind=kind, limit=limit, cursor=cursor
    )
    return NotificationPage(
        items=[item for item in rows],
        next_cursor=next_cursor,
        unread_count=unread_count,
    )


@router.post(
    "/{notification_id}/read",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Mark a notification read",
)
async def mark_read(notification_id: uuid.UUID, principal: PrincipalDep, db: DbDep) -> Response:
    await service.mark_read(db, principal, notification_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/read-all",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Mark all notifications read",
)
async def mark_all_read(principal: PrincipalDep, db: DbDep) -> Response:
    await service.mark_all_read(db, principal)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get(
    "/stream",
    summary="Notification stream (SSE)",
    response_class=StreamingResponse,
)
async def stream_notifications(
    request: Request,
    principal: PrincipalDep,
    settings: SettingsDep,
    last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
) -> StreamingResponse:
    return StreamingResponse(
        notification_stream(
            request=request,
            principal=principal,
            settings=settings,
            start_after=last_event_id or "",
        ),
        media_type="text/event-stream",
        headers=SSE_HEADERS,
    )
