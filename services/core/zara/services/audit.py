"""Append-only audit trail with a per-tenant SHA-256 hash chain.

Rows are written under ``zara_audit_writer`` (INSERT only) inside the caller's
transaction. A transaction-scoped advisory lock serialises the chain for a
tenant, so concurrent writers cannot fork it.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import insert, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from zara.auth.principal import Principal
from zara.db.base import new_uuid7
from zara.models import AuditLog
from zara.net import sanitize_ip
from zara.observability.context import get_request_id

_WRITER = "SET LOCAL ROLE zara_audit_writer"
_APP = "SET LOCAL ROLE zara_app"


def _canonical(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")


def _row_payload(
    *,
    row_id: uuid.UUID,
    tenant_id: uuid.UUID,
    user_id: uuid.UUID | None,
    actor_type: str,
    actor_id: str | None,
    action: str,
    resource_type: str | None,
    resource_id: str | None,
    run_id: uuid.UUID | None,
    metadata: dict[str, Any] | None,
    ip: str | None,
    trace_id: str | None,
    created_at: datetime,
) -> dict[str, Any]:
    return {
        "id": str(row_id),
        "tenant_id": str(tenant_id),
        "user_id": str(user_id) if user_id else None,
        "actor_type": actor_type,
        "actor_id": actor_id,
        "action": action,
        "resource_type": resource_type,
        "resource_id": resource_id,
        "run_id": str(run_id) if run_id else None,
        "metadata": metadata,
        "ip": ip,
        "trace_id": trace_id,
        "created_at": created_at.isoformat(),
    }


def _hash(prev_hash: bytes | None, payload: dict[str, Any]) -> bytes:
    return hashlib.sha256((prev_hash or b"") + _canonical(payload)).digest()


async def record_event(
    db: AsyncSession,
    principal: Principal,
    *,
    action: str,
    resource_type: str | None = None,
    resource_id: str | None = None,
    run_id: uuid.UUID | None = None,
    metadata: dict[str, Any] | None = None,
    ip: str | None = None,
    actor_type: str = "user",
) -> AuditLog:
    """Append one audit row inside the caller's transaction."""
    ip = sanitize_ip(ip)
    await db.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
        {"key": f"audit:{principal.tenant_id}"},
    )
    last = (
        await db.execute(
            select(AuditLog)
            .where(AuditLog.tenant_id == principal.tenant_id)
            .order_by(AuditLog.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()

    row_id = new_uuid7()
    created_at = datetime.now(UTC)
    payload = _row_payload(
        row_id=row_id,
        tenant_id=principal.tenant_id,
        user_id=principal.user_id,
        actor_type=actor_type,
        actor_id=str(principal.user_id),
        action=action,
        resource_type=resource_type,
        resource_id=resource_id,
        run_id=run_id,
        metadata=metadata,
        ip=ip,
        trace_id=get_request_id(),
        created_at=created_at,
    )
    prev_hash = last.row_hash if last is not None else None
    row = AuditLog(
        id=row_id,
        tenant_id=principal.tenant_id,
        user_id=principal.user_id,
        actor_type=actor_type,
        actor_id=str(principal.user_id),
        action=action,
        resource_type=resource_type,
        resource_id=resource_id,
        run_id=run_id,
        metadata_json=metadata,
        ip=ip,
        trace_id=get_request_id(),
        prev_hash=prev_hash,
        row_hash=_hash(prev_hash, payload),
        created_at=created_at,
    )
    # Insert with a Core statement: an ORM flush under the writer role would
    # also try to persist unrelated pending business rows, which that role
    # cannot touch.
    await db.execute(text(_WRITER))
    try:
        await db.execute(
            insert(AuditLog).values(
                id=row_id,
                tenant_id=principal.tenant_id,
                user_id=principal.user_id,
                actor_type=actor_type,
                actor_id=str(principal.user_id),
                action=action,
                resource_type=resource_type,
                resource_id=resource_id,
                run_id=run_id,
                metadata_json=metadata,
                ip=ip,
                trace_id=get_request_id(),
                prev_hash=prev_hash,
                row_hash=_hash(prev_hash, payload),
                created_at=created_at,
            )
        )
    finally:
        await db.execute(text(_APP))
    return row


async def verify_chain(db: AsyncSession, tenant_id: uuid.UUID) -> tuple[bool, list[uuid.UUID]]:
    """Recompute a tenant's chain; return validity and any broken row ids."""
    rows = list(
        (
            await db.execute(
                select(AuditLog).where(AuditLog.tenant_id == tenant_id).order_by(AuditLog.id.asc())
            )
        ).scalars()
    )
    prev: bytes | None = None
    broken: list[uuid.UUID] = []
    for row in rows:
        payload = _row_payload(
            row_id=row.id,
            tenant_id=row.tenant_id,
            user_id=row.user_id,
            actor_type=row.actor_type,
            actor_id=row.actor_id,
            action=row.action,
            resource_type=row.resource_type,
            resource_id=row.resource_id,
            run_id=row.run_id,
            metadata=row.metadata_json,
            ip=row.ip,
            trace_id=row.trace_id,
            created_at=row.created_at,
        )
        if row.prev_hash != prev or row.row_hash != _hash(prev, payload):
            broken.append(row.id)
        prev = row.row_hash
    return not broken, broken
