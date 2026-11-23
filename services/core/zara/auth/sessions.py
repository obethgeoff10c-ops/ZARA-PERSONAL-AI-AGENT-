"""Server-side sessions for BFF cookies, listing, and sign-out-everywhere."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from zara.auth.principal import Principal
from zara.auth.tokens import hash_session_token, mint_session_token
from zara.config import Settings
from zara.models import AuthSession
from zara.net import sanitize_ip


async def create_session(
    db: AsyncSession,
    principal: Principal,
    settings: Settings,
    *,
    device_label: str | None,
    ip: str | None,
    user_agent: str | None,
) -> tuple[AuthSession, str]:
    token = mint_session_token()
    now = datetime.now(UTC)
    auth_session = AuthSession(
        tenant_id=principal.tenant_id,
        user_id=principal.user_id,
        session_token_hash=hash_session_token(token),
        ip=sanitize_ip(ip),
        user_agent=user_agent,
        device_label=device_label,
        created_at=now,
        last_used_at=now,
        expires_at=now + timedelta(seconds=settings.auth_session_ttl_seconds),
    )
    db.add(auth_session)
    await db.flush()
    return auth_session, token


async def list_sessions(db: AsyncSession, principal: Principal) -> list[AuthSession]:
    now = datetime.now(UTC)
    result = await db.execute(
        select(AuthSession)
        .where(
            AuthSession.tenant_id == principal.tenant_id,
            AuthSession.user_id == principal.user_id,
            AuthSession.revoked_at.is_(None),
            AuthSession.expires_at > now,
        )
        .order_by(AuthSession.last_used_at.desc().nullslast(), AuthSession.created_at.desc())
    )
    return list(result.scalars())


async def revoke_session(
    db: AsyncSession,
    principal: Principal,
    session_id: uuid.UUID,
    *,
    reason: str = "user_requested",
) -> bool:
    result = await db.execute(
        select(AuthSession).where(
            AuthSession.id == session_id,
            AuthSession.tenant_id == principal.tenant_id,
            AuthSession.user_id == principal.user_id,
        )
    )
    auth_session = result.scalar_one_or_none()
    if auth_session is None:
        return False
    if auth_session.revoked_at is None:
        auth_session.revoked_at = datetime.now(UTC)
        auth_session.revoke_reason = reason
    return True


async def revoke_all_sessions(
    db: AsyncSession,
    principal: Principal,
    *,
    reason: str = "user_requested",
) -> None:
    """Revoke every active session owned by ``principal``."""
    await db.execute(
        update(AuthSession)
        .where(
            AuthSession.tenant_id == principal.tenant_id,
            AuthSession.user_id == principal.user_id,
            AuthSession.revoked_at.is_(None),
        )
        .values(revoked_at=datetime.now(UTC), revoke_reason=reason)
    )


async def revoke_current(db: AsyncSession, principal: Principal) -> None:
    """Sign the caller out.

    A session-token caller is signed out of that one session. A JWT/IdP caller
    has no single session id, so signing out revokes *all* of the user's active
    sessions instead of silently doing nothing.
    """
    if principal.session_id is not None:
        await revoke_session(db, principal, principal.session_id, reason="sign_out")
        return
    await revoke_all_sessions(db, principal, reason="sign_out")
