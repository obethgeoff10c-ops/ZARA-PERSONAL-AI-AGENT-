"""Agent-run ledger, tool history, and SSE event replay (07-API.md §3)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Header, Query, Request, status
from fastapi.responses import StreamingResponse

from zara.api.deps import (
    DbDep,
    PrincipalDep,
    SettingsDep,
    UserRateLimit,
    client_ip,
    parse_uuid,
)
from zara.api.pagination import MAX_PAGE_SIZE
from zara.api.sse import SSE_HEADERS, run_event_stream
from zara.db.session import rls_session
from zara.errors import validation_failed
from zara.schemas import (
    Page,
    RunCancelIn,
    RunCancelOut,
    RunCreate,
    RunOut,
    ToolExecutionOut,
)
from zara.services import runs as service

router = APIRouter(prefix="/runs", tags=["runs"])

_CreateLimit = UserRateLimit("runs-create", limit=30)


@router.post(
    "",
    response_model=RunOut,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Queue an agent run",
    dependencies=[Depends(_CreateLimit)],
)
async def create_run(
    payload: RunCreate,
    principal: PrincipalDep,
    db: DbDep,
    settings: SettingsDep,
    request: Request,
) -> RunOut:
    run = await service.create_run(
        db,
        principal,
        goal=payload.goal,
        domains=payload.domains,
        conversation_id=payload.conversation_id,
        mode=payload.mode,
        budget=payload.budget,
        ip=client_ip(request, settings),
    )
    return RunOut.model_validate(run)


@router.get("", response_model=Page[RunOut], summary="List runs")
async def list_runs(
    principal: PrincipalDep,
    db: DbDep,
    status_filter: Annotated[str | None, Query(alias="status")] = None,
    trigger_type: str | None = None,
    since: datetime | None = None,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = 50,
    cursor: str | None = None,
) -> Page[RunOut]:
    rows, next_cursor = await service.list_runs(
        db,
        principal,
        status=status_filter,
        trigger_type=trigger_type,
        since=since,
        limit=limit,
        cursor=cursor,
    )
    return Page(items=[RunOut.model_validate(row) for row in rows], next_cursor=next_cursor)


@router.get("/{run_id}", response_model=RunOut, summary="Get a run")
async def get_run(run_id: uuid.UUID, principal: PrincipalDep, db: DbDep) -> RunOut:
    return RunOut.model_validate(await service.get_run(db, principal, run_id))


@router.post(
    "/{run_id}/cancel",
    response_model=RunCancelOut,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Cancel a run",
)
async def cancel_run(
    run_id: uuid.UUID,
    payload: RunCancelIn,
    principal: PrincipalDep,
    db: DbDep,
    settings: SettingsDep,
    request: Request,
) -> RunCancelOut:
    run = await service.cancel_run(
        db, principal, run_id, reason=payload.reason, ip=client_ip(request, settings)
    )
    return RunCancelOut(status=run.status)


@router.get(
    "/{run_id}/tools",
    response_model=Page[ToolExecutionOut],
    summary="Tool executions for a run",
)
async def list_tools(
    run_id: uuid.UUID, principal: PrincipalDep, db: DbDep
) -> Page[ToolExecutionOut]:
    rows = await service.list_tools(db, principal, run_id)
    return Page(items=[ToolExecutionOut.model_validate(row) for row in rows])


@router.get(
    "/{run_id}/events",
    summary="Stream run events (SSE with Last-Event-ID replay)",
    response_class=StreamingResponse,
)
async def stream_events(
    run_id: uuid.UUID,
    request: Request,
    principal: PrincipalDep,
    settings: SettingsDep,
    last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
) -> StreamingResponse:
    run_uuid = parse_uuid(str(run_id), field="run_id")
    start_after = 0
    if last_event_id:
        try:
            start_after = max(0, int(last_event_id))
        except ValueError:
            raise validation_failed(
                "Last-Event-ID must be the numeric event sequence.",
                errors=[{"field": "Last-Event-ID", "message": "Expected an integer"}],
            ) from None

    async with rls_session(
        tenant_id=principal.tenant_id, user_id=principal.user_id, role="user"
    ) as session:
        run = await service.get_run(session, principal, run_uuid)
        run_status = run.status

    return StreamingResponse(
        run_event_stream(
            request=request,
            principal=principal,
            settings=settings,
            run_id=run_uuid,
            run_status=run_status,
            start_after=start_after,
        ),
        media_type="text/event-stream",
        headers=SSE_HEADERS,
    )
