"""Shared FastAPI dependencies: settings, identity, RLS sessions, rate limits."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, Request, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from zara.auth.principal import Principal
from zara.auth.service import authenticate
from zara.auth.tokens import step_up_is_fresh
from zara.config import Settings, get_settings
from zara.db.session import rls_session
from zara.errors import step_up_required, validation_failed
from zara.services.ratelimit import RateLimiter, RateLimitResult

_bearer = HTTPBearer(auto_error=False, description="IdP access JWT or opaque session token")


def get_app_settings(request: Request) -> Settings:
    """Prefer the settings bound to the app instance (supports test overrides)."""
    settings = getattr(request.app.state, "settings", None)
    if isinstance(settings, Settings):
        return settings
    return get_settings()


def get_rate_limiter(request: Request) -> RateLimiter:
    limiter = getattr(request.app.state, "rate_limiter", None)
    if not isinstance(limiter, RateLimiter):
        limiter = RateLimiter()
        request.app.state.rate_limiter = limiter
    return limiter


def client_ip(request: Request, settings: Settings | None = None) -> str:
    resolved = settings or get_app_settings(request)
    if resolved.trust_proxy_headers:
        forwarded = request.headers.get("X-Forwarded-For")
        if forwarded:
            return forwarded.split(",", 1)[0].strip()
    return request.client.host if request.client else "unknown"


async def get_principal(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)] = None,
) -> Principal:
    settings = get_app_settings(request)
    authorization = (
        f"{credentials.scheme} {credentials.credentials}"
        if credentials is not None
        else request.headers.get("Authorization")
    )
    return await authenticate(authorization, settings)


async def get_db(
    principal: Annotated[Principal, Depends(get_principal)],
) -> AsyncIterator[AsyncSession]:
    async with rls_session(
        tenant_id=principal.tenant_id,
        user_id=principal.user_id,
        role="service" if principal.is_service else "user",
    ) as session:
        yield session


def require_step_up(principal: Principal) -> None:
    if not step_up_is_fresh(principal.authenticated_at):
        raise step_up_required()


SettingsDep = Annotated[Settings, Depends(get_app_settings)]
PrincipalDep = Annotated[Principal, Depends(get_principal)]
DbDep = Annotated[AsyncSession, Depends(get_db)]


@dataclass(frozen=True, slots=True)
class IpRateLimit:
    """Per-IP limiter for unauthenticated/abuse-prone endpoints."""

    scope: str
    limit: int
    window_seconds: int = 60

    async def __call__(
        self,
        request: Request,
        response: Response,
        settings: SettingsDep,
    ) -> None:
        await _enforce(
            request,
            response,
            settings,
            key=f"ip:{client_ip(request, settings)}:{self.scope}",
            limit=self.limit,
            window=self.window_seconds,
        )


@dataclass(frozen=True, slots=True)
class UserRateLimit:
    """Per-user limiter for authenticated operations."""

    scope: str
    limit: int
    window_seconds: int = 60

    async def __call__(
        self,
        request: Request,
        response: Response,
        settings: SettingsDep,
        principal: PrincipalDep,
    ) -> None:
        await _enforce(
            request,
            response,
            settings,
            key=f"user:{principal.user_id}:{self.scope}",
            limit=self.limit,
            window=self.window_seconds,
        )


async def _enforce(
    request: Request,
    response: Response,
    settings: Settings,
    *,
    key: str,
    limit: int,
    window: int,
) -> None:
    if not settings.rate_limit_enabled:
        return
    result: RateLimitResult = await get_rate_limiter(request).hit(
        key,
        limit=limit,
        window_seconds=window,
        redis_url=settings.redis_url_str,
    )
    for header, value in result.headers.items():
        response.headers[header] = value


def parse_uuid(value: str, *, field: str = "id") -> uuid.UUID:
    try:
        return uuid.UUID(value)
    except ValueError:
        raise validation_failed(
            f"'{field}' must be a valid UUID.",
            errors=[{"field": field, "message": "Invalid UUID"}],
        ) from None


def parse_if_match(request: Request, *, field: str = "If-Match") -> int | None:
    raw = request.headers.get("If-Match")
    if raw is None or raw.strip() == "":
        return None
    candidate = raw.strip().strip('"').removeprefix("W/").strip('"')
    try:
        return int(candidate)
    except ValueError:
        raise validation_failed(
            "If-Match must carry the resource version.",
            errors=[{"field": field, "message": "Expected an integer version"}],
        ) from None
