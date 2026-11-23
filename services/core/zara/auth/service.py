"""Identity resolution and just-in-time provisioning.

Users are provisioned from verified IdP claims on first sight. The provisioning
transaction runs under ``zara_provisioner`` (BYPASSRLS) because no tenant context
can exist yet; every other query runs under the caller's RLS context.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from zara.auth.principal import Principal
from zara.auth.tokens import (
    TokenClaims,
    hash_session_token,
    is_session_token,
    require_bearer,
    verify_access_token,
)
from zara.config import Settings
from zara.db.session import session_scope
from zara.errors import forbidden, unauthenticated
from zara.models import AuthSession, Tenant, User, UserProfile

_PROVISIONER = "SET LOCAL ROLE zara_provisioner"


def _tenant_slug(claims: TokenClaims) -> tuple[str, str]:
    key = claims.org_id or f"{claims.raw.get('iss', 'idp')}:{claims.subject}"
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:20]
    if claims.org_id:
        return f"org-{digest}", "Organization"
    domain = (claims.email or "").partition("@")[2] or "Personal"
    return f"personal-{digest}", domain


async def _find_or_provision(claims: TokenClaims, provider: str) -> Principal:
    async with session_scope() as session:
        await session.execute(text(_PROVISIONER))

        user = (
            await session.execute(
                select(User).where(
                    User.auth_provider == provider,
                    User.external_auth_id == claims.subject,
                )
            )
        ).scalar_one_or_none()

        if user is None:
            # Serialize first-login races for the same external identity.
            lock_key = f"{provider}:{claims.subject}"
            await session.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
                {"key": lock_key},
            )
            user = (
                await session.execute(
                    select(User).where(
                        User.auth_provider == provider,
                        User.external_auth_id == claims.subject,
                    )
                )
            ).scalar_one_or_none()

        if user is None:
            user = await _create_user(session, claims, provider)
        elif user.status != "active":
            raise forbidden("This account is not active.")

        now = datetime.now(UTC)
        user.last_seen_at = now
        tenant_id = user.tenant_id

        return Principal(
            tenant_id=tenant_id,
            user_id=user.id,
            role=user.role,
            email=user.email,
            display_name=user.display_name,
            auth_provider=user.auth_provider,
            external_auth_id=user.external_auth_id,
            authenticated_at=claims.authenticated_at,
            mfa=claims.mfa,
            scopes=claims.scopes,
        )


async def _create_user(session: AsyncSession, claims: TokenClaims, provider: str) -> User:
    slug, tenant_name = _tenant_slug(claims)
    tenant = (await session.execute(select(Tenant).where(Tenant.slug == slug))).scalar_one_or_none()
    is_new_tenant = tenant is None
    if tenant is None:
        tenant = Tenant(name=tenant_name, slug=slug, plan="free", status="active")
        session.add(tenant)
        await session.flush()

    email = claims.email or f"{claims.subject}@{provider}.invalid"
    user = User(
        tenant_id=tenant.id,
        external_auth_id=claims.subject,
        auth_provider=provider,
        email=email,
        email_verified=claims.email_verified,
        display_name=claims.display_name,
        role="owner" if is_new_tenant else "member",
        status="active",
        last_seen_at=datetime.now(UTC),
    )
    session.add(user)
    await session.flush()
    session.add(UserProfile(user_id=user.id, tenant_id=tenant.id))
    await session.flush()
    return user


async def principal_from_session_token(token: str) -> Principal:
    digest = hash_session_token(token)
    async with session_scope() as session:
        await session.execute(text(_PROVISIONER))
        row = (
            await session.execute(
                select(AuthSession, User)
                .join(User, AuthSession.user_id == User.id)
                .where(AuthSession.session_token_hash == digest)
            )
        ).one_or_none()
        if row is None:
            raise unauthenticated("The session token is not valid.")
        auth_session, user = row
        now = datetime.now(UTC)
        if auth_session.revoked_at is not None:
            raise unauthenticated("This session has been revoked.")
        if auth_session.expires_at <= now:
            raise unauthenticated("This session has expired.")
        if user.status != "active":
            raise forbidden("This account is not active.")
        auth_session.last_used_at = now
        user.last_seen_at = now
        return Principal(
            tenant_id=user.tenant_id,
            user_id=user.id,
            role=user.role,
            email=user.email,
            display_name=user.display_name,
            auth_provider=user.auth_provider,
            external_auth_id=user.external_auth_id,
            session_id=auth_session.id,
            # The session's creation time is the last moment the user proved their
            # identity; step-up checks compare against it.
            authenticated_at=auth_session.created_at,
        )


async def authenticate(authorization: str | None, settings: Settings) -> Principal:
    token = require_bearer(authorization)
    if is_session_token(token):
        return await principal_from_session_token(token)
    claims = await verify_access_token(token, settings)
    return await _find_or_provision(claims, settings.auth_provider)
