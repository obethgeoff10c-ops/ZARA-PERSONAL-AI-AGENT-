"""Approval decisions bound to the exact tool arguments.

Approvals are single-use, expire, and require fresh step-up authentication for
risk tier 4. The client must echo the current ``args_hash``; a mismatch is a 412.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from zara.api.deps import require_step_up
from zara.api.pagination import paginate
from zara.auth.principal import Principal
from zara.errors import conflict, not_found, precondition_failed, validation_failed
from zara.models import Approval, ToolExecution
from zara.services.audit import record_event


def args_hash_for(args: dict[str, Any]) -> bytes:
    return hashlib.sha256(
        json.dumps(args, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    ).digest()


async def list_approvals(
    db: AsyncSession,
    principal: Principal,
    *,
    status: str | None = None,
    limit: int = 50,
    cursor: str | None = None,
) -> tuple[list[Approval], str | None]:
    stmt = select(Approval).where(
        Approval.tenant_id == principal.tenant_id,
        Approval.user_id == principal.user_id,
    )
    if status == "pending":
        stmt = stmt.where(Approval.status == "pending")
    elif status == "decided":
        stmt = stmt.where(Approval.status.not_in(["pending"]))
    return await paginate(db, stmt, limit=limit, cursor=cursor, model=Approval)


async def get_approval(db: AsyncSession, principal: Principal, approval_id: uuid.UUID) -> Approval:
    approval = (
        await db.execute(
            select(Approval).where(
                Approval.id == approval_id,
                Approval.tenant_id == principal.tenant_id,
                Approval.user_id == principal.user_id,
            )
        )
    ).scalar_one_or_none()
    if approval is None:
        raise not_found("Approval not found.")
    return approval


def _ensure_decidable(approval: Approval) -> None:
    now = datetime.now(UTC)
    if approval.status != "pending":
        raise conflict(f"This approval is already {approval.status}.")
    if approval.expires_at <= now:
        raise precondition_failed("This approval request has expired.")


def _check_hash(approval: Approval, supplied: str) -> None:
    try:
        digest = bytes.fromhex(supplied)
    except ValueError:
        raise validation_failed(
            "args_hash must be lowercase hex.",
            errors=[{"field": "args_hash", "message": "Invalid hash encoding"}],
        ) from None
    if not hmac.compare_digest(digest, approval.args_hash):
        raise precondition_failed("args_hash does not match the arguments shown for this approval.")


async def approve(
    db: AsyncSession,
    principal: Principal,
    approval_id: uuid.UUID,
    *,
    args_hash: str,
    note: str | None,
    ip: str | None = None,
) -> Approval:
    approval = await get_approval(db, principal, approval_id)
    _ensure_decidable(approval)
    _check_hash(approval, args_hash)
    step_up = False
    if (approval.risk_tier or 0) >= 4:
        require_step_up(principal)
        step_up = True
    approval.status = "approved"
    approval.decided_at = datetime.now(UTC)
    approval.decided_via = "web"
    approval.decision_note = note
    approval.step_up_verified = step_up
    await db.flush()
    await record_event(
        db,
        principal,
        action="approval.approved",
        resource_type="approval",
        resource_id=str(approval.id),
        run_id=approval.run_id,
        metadata={"risk_tier": approval.risk_tier, "step_up": step_up},
        ip=ip,
    )
    return approval


async def edit_and_approve(
    db: AsyncSession,
    principal: Principal,
    approval_id: uuid.UUID,
    *,
    edits: dict[str, Any],
    note: str | None,
    ip: str | None = None,
) -> Approval:
    approval = await get_approval(db, principal, approval_id)
    _ensure_decidable(approval)
    if (approval.risk_tier or 0) >= 4:
        require_step_up(principal)

    execution = None
    if approval.tool_execution_id is not None:
        execution = (
            await db.execute(
                select(ToolExecution).where(
                    ToolExecution.id == approval.tool_execution_id,
                    ToolExecution.tenant_id == principal.tenant_id,
                    ToolExecution.user_id == principal.user_id,
                )
            )
        ).scalar_one_or_none()
        if execution is not None and execution.policy_decision == "deny":
            raise precondition_failed("Policy denies this tool; edits cannot override a denial.")

    base_args = dict(execution.args) if execution is not None else {}
    merged = {**base_args, **edits}
    approval.edited_args_hash = args_hash_for(merged)
    approval.status = "edited_approved"
    approval.decided_at = datetime.now(UTC)
    approval.decided_via = "web_edit"
    approval.decision_note = note
    approval.step_up_verified = (approval.risk_tier or 0) >= 4
    approval.preview = {**approval.preview, "edited_fields": sorted(edits)}
    await db.flush()
    await record_event(
        db,
        principal,
        action="approval.edited_approved",
        resource_type="approval",
        resource_id=str(approval.id),
        run_id=approval.run_id,
        metadata={"edited_fields": sorted(edits), "risk_tier": approval.risk_tier},
        ip=ip,
    )
    return approval


async def reject(
    db: AsyncSession,
    principal: Principal,
    approval_id: uuid.UUID,
    *,
    reason: str | None,
    ip: str | None = None,
) -> Approval:
    approval = await get_approval(db, principal, approval_id)
    _ensure_decidable(approval)
    approval.status = "rejected"
    approval.decided_at = datetime.now(UTC)
    approval.decided_via = "web"
    approval.decision_note = reason
    await db.flush()
    await record_event(
        db,
        principal,
        action="approval.rejected",
        resource_type="approval",
        resource_id=str(approval.id),
        run_id=approval.run_id,
        ip=ip,
    )
    return approval
