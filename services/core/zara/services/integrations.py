"""Integration metadata, settings, lifecycle, and scope enforcement."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from zara.auth.principal import Principal
from zara.errors import not_found, validation_failed
from zara.models import Integration, OAuthCredential
from zara.services.audit import record_event

_EDITABLE_KEYS: dict[str, set[str]] = {
    "gmail": {"labels_include", "labels_exclude", "backfill_days"},
    "gcal": {"calendar_ids", "backfill_days"},
    "gdrive": {"folder_ids", "backfill_days"},
    "github": {"repo_ids", "stale_pr_days"},
}


async def list_integrations(db: AsyncSession, principal: Principal) -> list[Integration]:
    result = await db.execute(
        select(Integration)
        .where(
            Integration.tenant_id == principal.tenant_id,
            Integration.user_id == principal.user_id,
        )
        .order_by(Integration.created_at.desc())
    )
    return list(result.scalars())


async def get_integration(
    db: AsyncSession, principal: Principal, integration_id: uuid.UUID
) -> Integration:
    integration = (
        await db.execute(
            select(Integration).where(
                Integration.id == integration_id,
                Integration.tenant_id == principal.tenant_id,
                Integration.user_id == principal.user_id,
            )
        )
    ).scalar_one_or_none()
    if integration is None:
        raise not_found("Integration not found.")
    return integration


async def update_settings(
    db: AsyncSession,
    principal: Principal,
    integration_id: uuid.UUID,
    settings: dict[str, Any],
    *,
    ip: str | None = None,
) -> Integration:
    integration = await get_integration(db, principal, integration_id)
    allowed = _EDITABLE_KEYS.get(integration.provider, set())
    unknown = sorted(set(settings) - allowed)
    if unknown:
        raise validation_failed(
            "Unsupported settings for this provider.",
            errors=[{"field": "settings", "message": f"Unknown keys: {unknown}"}],
        )
    integration.settings = {**integration.settings, **settings}
    await db.flush()
    await record_event(
        db,
        principal,
        action="integration.settings_updated",
        resource_type="integration",
        resource_id=str(integration.id),
        metadata={"keys": sorted(settings), "provider": integration.provider},
        ip=ip,
    )
    return integration


async def set_paused(
    db: AsyncSession,
    principal: Principal,
    integration_id: uuid.UUID,
    *,
    paused: bool,
    ip: str | None = None,
) -> Integration:
    integration = await get_integration(db, principal, integration_id)
    if integration.status == "revoked":
        raise validation_failed(
            "A revoked integration cannot be resumed.",
            errors=[{"field": "status", "message": "Integration is revoked"}],
        )
    integration.status = "paused" if paused else "active"
    await db.flush()
    await record_event(
        db,
        principal,
        action="integration.paused" if paused else "integration.resumed",
        resource_type="integration",
        resource_id=str(integration.id),
        metadata={"provider": integration.provider},
        ip=ip,
    )
    return integration


async def revoke(
    db: AsyncSession,
    principal: Principal,
    integration_id: uuid.UUID,
    *,
    ip: str | None = None,
) -> None:
    integration = await get_integration(db, principal, integration_id)
    integration.status = "revoked"
    integration.revoked_at = datetime.now(UTC)
    await db.execute(
        delete(OAuthCredential).where(
            OAuthCredential.integration_id == integration.id,
            OAuthCredential.tenant_id == principal.tenant_id,
        )
    )
    await db.flush()
    await record_event(
        db,
        principal,
        action="integration.revoked",
        resource_type="integration",
        resource_id=str(integration.id),
        metadata={"provider": integration.provider},
        ip=ip,
    )
