"""Agent-run ledger and persisted event stream.

This milestone owns run *state and transport*; execution workers are added with
the agent milestone. Creating a run records a real queued run (no simulated
agent output is produced).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from zara.api.pagination import paginate
from zara.auth.principal import Principal
from zara.errors import conflict, not_found, validation_failed
from zara.models import AgentRun, Conversation, RunEvent, ToolExecution
from zara.services.audit import record_event

TERMINAL_STATUSES = {"succeeded", "failed", "cancelled", "expired"}
_ALLOWED_DOMAINS = {
    "personal",
    "executive",
    "sales",
    "developer",
    "email",
    "calendar",
    "research",
}


async def get_run(db: AsyncSession, principal: Principal, run_id: uuid.UUID) -> AgentRun:
    run = (
        await db.execute(
            select(AgentRun).where(
                AgentRun.id == run_id,
                AgentRun.tenant_id == principal.tenant_id,
                AgentRun.user_id == principal.user_id,
            )
        )
    ).scalar_one_or_none()
    if run is None:
        raise not_found("Run not found.")
    return run


async def list_runs(
    db: AsyncSession,
    principal: Principal,
    *,
    status: str | None = None,
    trigger_type: str | None = None,
    since: datetime | None = None,
    limit: int = 50,
    cursor: str | None = None,
) -> tuple[list[AgentRun], str | None]:
    stmt = select(AgentRun).where(
        AgentRun.tenant_id == principal.tenant_id,
        AgentRun.user_id == principal.user_id,
    )
    if status:
        stmt = stmt.where(AgentRun.status == status)
    if trigger_type:
        stmt = stmt.where(AgentRun.trigger_type == trigger_type)
    if since:
        stmt = stmt.where(AgentRun.created_at >= since)
    return await paginate(db, stmt, limit=limit, cursor=cursor, model=AgentRun)


async def create_run(
    db: AsyncSession,
    principal: Principal,
    *,
    goal: str,
    domains: list[str] | None,
    conversation_id: uuid.UUID | None,
    mode: str | None,
    budget: dict[str, Any] | None,
    ip: str | None = None,
) -> AgentRun:
    if domains:
        unknown = sorted(set(domains) - _ALLOWED_DOMAINS)
        if unknown:
            raise validation_failed(
                "Unknown domains.",
                errors=[{"field": "domains", "message": f"Unsupported: {unknown}"}],
            )
    if conversation_id is not None:
        owned = await db.scalar(
            select(Conversation.id).where(
                Conversation.id == conversation_id,
                Conversation.tenant_id == principal.tenant_id,
                Conversation.user_id == principal.user_id,
            )
        )
        if owned is None:
            raise validation_failed(
                "The conversation does not exist.",
                errors=[{"field": "conversation_id", "message": "Unknown conversation"}],
            )

    run = AgentRun(
        tenant_id=principal.tenant_id,
        user_id=principal.user_id,
        conversation_id=conversation_id,
        trigger_type="user_message" if conversation_id else "manual",
        goal=goal,
        status="queued",
        domains=domains,
        budget=budget,
        autonomy_level="suggest",
    )
    db.add(run)
    await db.flush()
    await _append_event(db, run, "run.status", {"status": "queued"})
    await record_event(
        db,
        principal,
        action="run.created",
        resource_type="agent_run",
        resource_id=str(run.id),
        run_id=run.id,
        metadata={"domains": domains, "mode": mode},
        ip=ip,
    )
    return run


async def cancel_run(
    db: AsyncSession,
    principal: Principal,
    run_id: uuid.UUID,
    *,
    reason: str | None,
    ip: str | None = None,
) -> AgentRun:
    run = await get_run(db, principal, run_id)
    if run.status in TERMINAL_STATUSES:
        raise conflict(f"Run is already {run.status}.")
    run.status = "cancelled"
    run.finished_at = datetime.now(UTC)
    run.error = {"code": "cancelled", "message": reason or "Cancelled by user"}
    await db.flush()
    await _append_event(db, run, "run.status", {"status": "cancelled", "reason": reason})
    await record_event(
        db,
        principal,
        action="run.cancelled",
        resource_type="agent_run",
        resource_id=str(run.id),
        run_id=run.id,
        ip=ip,
    )
    return run


async def list_tools(
    db: AsyncSession, principal: Principal, run_id: uuid.UUID
) -> list[ToolExecution]:
    await get_run(db, principal, run_id)
    result = await db.execute(
        select(ToolExecution)
        .where(
            ToolExecution.run_id == run_id,
            ToolExecution.tenant_id == principal.tenant_id,
            ToolExecution.user_id == principal.user_id,
        )
        .order_by(ToolExecution.created_at.asc())
    )
    return list(result.scalars())


async def event_replay_floor(db: AsyncSession, principal: Principal, run_id: uuid.UUID) -> int:
    await get_run(db, principal, run_id)
    maximum = await db.scalar(select(func.max(RunEvent.seq)).where(RunEvent.run_id == run_id))
    return int(maximum or 0)


async def _append_event(
    db: AsyncSession, run: AgentRun, event_type: str, data: dict[str, Any]
) -> RunEvent:
    await db.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
        {"key": f"run-events:{run.id}"},
    )
    current = await db.scalar(select(func.max(RunEvent.seq)).where(RunEvent.run_id == run.id))
    event = RunEvent(
        tenant_id=run.tenant_id,
        run_id=run.id,
        seq=int(current or 0) + 1,
        type=event_type,
        data=data,
    )
    db.add(event)
    await db.flush()
    return event
