"""User profile, preferences, and permission reads/writes."""

from __future__ import annotations

from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from zara.auth.principal import Principal
from zara.models import User, UserPreference, UserProfile
from zara.services.audit import record_event
from zara.services.permissions import permissions_for


async def get_user_with_profile(db: AsyncSession, principal: Principal) -> tuple[User, UserProfile]:
    user = (
        await db.execute(
            select(User).where(
                User.id == principal.user_id,
                User.tenant_id == principal.tenant_id,
            )
        )
    ).scalar_one_or_none()
    profile = (
        await db.execute(
            select(UserProfile).where(
                UserProfile.user_id == principal.user_id,
                UserProfile.tenant_id == principal.tenant_id,
            )
        )
    ).scalar_one_or_none()
    if user is None:
        # The principal was provisioned in a prior transaction; a missing row
        # here means the account was deleted concurrently.
        from zara.errors import unauthenticated

        raise unauthenticated("The account no longer exists.")
    if profile is None:
        profile = UserProfile(user_id=user.id, tenant_id=principal.tenant_id)
        db.add(profile)
        await db.flush()
    return user, profile


async def update_profile(
    db: AsyncSession,
    principal: Principal,
    changes: dict[str, Any],
    *,
    ip: str | None = None,
) -> UserProfile:
    user, profile = await get_user_with_profile(db, principal)
    if "display_name" in changes:
        user.display_name = changes.pop("display_name")
    for field, value in changes.items():
        if value is not None:
            setattr(profile, field, value)
    await db.flush()
    await record_event(
        db,
        principal,
        action="user.profile_updated",
        resource_type="user",
        resource_id=str(principal.user_id),
        metadata={"fields": sorted(changes)},
        ip=ip,
    )
    return profile


async def get_preferences(db: AsyncSession, principal: Principal, namespace: str) -> dict[str, Any]:
    rows = list(
        (
            await db.execute(
                select(UserPreference).where(
                    UserPreference.tenant_id == principal.tenant_id,
                    UserPreference.user_id == principal.user_id,
                    UserPreference.namespace == namespace,
                )
            )
        ).scalars()
    )
    return {row.key: row.value for row in rows}


async def put_preferences(
    db: AsyncSession,
    principal: Principal,
    namespace: str,
    values: dict[str, Any],
    *,
    ip: str | None = None,
) -> dict[str, Any]:
    existing = {
        row.key: row
        for row in (
            await db.execute(
                select(UserPreference).where(
                    UserPreference.tenant_id == principal.tenant_id,
                    UserPreference.user_id == principal.user_id,
                    UserPreference.namespace == namespace,
                )
            )
        ).scalars()
    }
    stale = set(existing) - set(values)
    if stale:
        await db.execute(
            delete(UserPreference).where(
                UserPreference.tenant_id == principal.tenant_id,
                UserPreference.user_id == principal.user_id,
                UserPreference.namespace == namespace,
                UserPreference.key.in_(stale),
            )
        )
    for key, value in values.items():
        row = existing.get(key)
        if row is None:
            db.add(
                UserPreference(
                    tenant_id=principal.tenant_id,
                    user_id=principal.user_id,
                    namespace=namespace,
                    key=key,
                    value=value,
                    source="user",
                )
            )
        else:
            row.value = value
            row.source = "user"
    await db.flush()
    await record_event(
        db,
        principal,
        action="user.preferences_updated",
        resource_type="user_preference",
        resource_id=f"{principal.user_id}:{namespace}",
        metadata={"namespace": namespace, "keys": sorted(values)},
        ip=ip,
    )
    return values


def permission_list(principal: Principal) -> list[str]:
    return sorted(permissions_for(principal.role))
