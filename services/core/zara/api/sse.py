"""Server-Sent Events transport with replay, heartbeat, and cancellation.

Events are persisted in ``agent.run_events`` before being emitted, so a
reconnect with ``Last-Event-ID`` resumes exactly after the last delivered event.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from typing import Any

from fastapi import Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from zara.auth.principal import Principal
from zara.config import Settings
from zara.db.session import rls_session
from zara.models import RunEvent

logger = logging.getLogger(__name__)

_TERMINAL_RUN_STATUSES = {"succeeded", "failed", "cancelled", "expired"}

SSE_HEADERS = {
    "Cache-Control": "no-cache, no-store",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}


def format_event(*, event: str, data: dict[str, Any], event_id: str | int | None = None) -> str:
    lines = []
    if event_id is not None:
        lines.append(f"id: {event_id}")
    lines.append(f"event: {event}")
    lines.append(f"data: {json.dumps(data, separators=(',', ':'), default=str)}")
    return "\n".join(lines) + "\n\n"


async def run_event_stream(
    *,
    request: Request,
    principal: Principal,
    settings: Settings,
    run_id: Any,
    run_status: str,
    start_after: int,
) -> AsyncIterator[str]:
    """Replay persisted events, then follow new ones until the run is terminal."""
    yield "retry: 3000\n\n"
    sequence = start_after
    status = run_status
    loop = asyncio.get_running_loop()
    last_heartbeat = loop.time()
    while True:
        if await request.is_disconnected():
            return
        try:
            events, status = await _load_events(principal, run_id, sequence)
        except Exception:
            logger.exception("SSE event read failed", extra={"event": "sse.read.failed"})
            yield format_event(
                event="error",
                data={
                    "code": "internal_error",
                    "message": "The event stream is temporarily unavailable.",
                    "retryable": True,
                },
            )
            return

        for run_event in events:
            sequence = run_event.seq
            yield format_event(event=run_event.type, data=run_event.data, event_id=run_event.seq)

        if status in _TERMINAL_RUN_STATUSES:
            yield format_event(event="done", data={"run_id": str(run_id), "status": status})
            return

        now = loop.time()
        if now - last_heartbeat >= settings.sse_heartbeat_seconds:
            last_heartbeat = now
            yield format_event(event="heartbeat", data={})
        try:
            await asyncio.sleep(settings.sse_poll_seconds)
        except asyncio.CancelledError:
            return


async def _load_events(principal: Principal, run_id: Any, after: int) -> tuple[list[RunEvent], str]:
    async with rls_session(
        tenant_id=principal.tenant_id, user_id=principal.user_id, role="user"
    ) as session:
        status = await _run_status(session, run_id)
        result = await session.execute(
            select(RunEvent)
            .where(RunEvent.run_id == run_id, RunEvent.seq > after)
            .order_by(RunEvent.seq.asc())
            .limit(200)
        )
        return list(result.scalars()), status


async def _run_status(session: AsyncSession, run_id: Any) -> str:
    from zara.models import AgentRun

    result = await session.execute(select(AgentRun.status).where(AgentRun.id == run_id))
    return str(result.scalar_one_or_none() or "unknown")


async def notification_stream(
    *,
    request: Request,
    principal: Principal,
    settings: Settings,
    start_after: str,
) -> AsyncIterator[str]:
    """Follow the caller's notifications. ``Last-Event-ID`` is an ISO timestamp."""
    from datetime import datetime

    from sqlalchemy import and_

    from zara.models import Notification

    yield "retry: 3000\n\n"
    cursor: datetime | None = None
    if start_after:
        try:
            cursor = datetime.fromisoformat(start_after)
        except ValueError:
            cursor = None

    last_heartbeat = asyncio.get_running_loop().time()
    while True:
        if await request.is_disconnected():
            return
        try:
            async with rls_session(
                tenant_id=principal.tenant_id, user_id=principal.user_id, role="user"
            ) as session:
                stmt = select(Notification).where(Notification.user_id == principal.user_id)
                if cursor is not None:
                    stmt = stmt.where(
                        and_(
                            Notification.created_at > cursor,
                            Notification.id.is_not(None),
                        )
                    )
                stmt = stmt.order_by(Notification.created_at.asc()).limit(100)
                rows = list((await session.execute(stmt)).scalars())
                for row in rows:
                    cursor = row.created_at
                    yield format_event(
                        event="notification.created",
                        data={
                            "id": str(row.id),
                            "kind": row.kind,
                            "title": row.title,
                            "body": row.body,
                            "priority": row.priority,
                            "created_at": row.created_at.isoformat(),
                        },
                        event_id=row.created_at.isoformat(),
                    )
        except Exception:
            logger.exception("notification SSE failed", extra={"event": "sse.notifications.failed"})
            yield format_event(
                event="error",
                data={
                    "code": "internal_error",
                    "message": "The notification stream is temporarily unavailable.",
                    "retryable": True,
                },
            )
            return
        now = asyncio.get_running_loop().time()
        if now - last_heartbeat >= settings.sse_heartbeat_seconds:
            last_heartbeat = now
            yield format_event(event="heartbeat", data={})
        try:
            await asyncio.sleep(settings.sse_poll_seconds)
        except asyncio.CancelledError:
            return
