"""Authentication, session, and identity endpoints (07-API.md §1)."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Request, Response, status

from zara.api.deps import (
    DbDep,
    IpRateLimit,
    PrincipalDep,
    SettingsDep,
    client_ip,
)
from zara.auth.sessions import create_session, list_sessions, revoke_current, revoke_session
from zara.auth.tokens import issue_local_token
from zara.errors import not_found
from zara.schemas import (
    AuthConfigOut,
    DevTokenIn,
    DevTokenOut,
    SessionCreateIn,
    SessionItem,
    SessionOut,
    UserOut,
)
from zara.services import users as user_service
from zara.services.audit import record_event

router = APIRouter(prefix="/auth", tags=["auth"])

_DEV_TOKEN_LIMIT = IpRateLimit("auth-token", limit=30)
_SESSION_LIMIT = IpRateLimit("auth-session", limit=30)


def _user_out(user: object, profile: object | None) -> UserOut:
    from zara.models import User, UserProfile

    assert isinstance(user, User)
    return UserOut(
        id=user.id,
        tenant_id=user.tenant_id,
        email=user.email,
        display_name=user.display_name,
        role=user.role,
        status=user.status,
        created_at=user.created_at,
        profile=profile if isinstance(profile, UserProfile) else None,
    )


@router.get("/config", response_model=AuthConfigOut, summary="Public auth configuration")
async def auth_config(settings: SettingsDep) -> AuthConfigOut:
    return AuthConfigOut(
        provider=settings.auth_provider,
        issuer=settings.auth_issuer,
        client_id=settings.auth_client_id,
        jwks_uri=str(settings.auth_jwks_url) if settings.auth_jwks_url else None,
        local_mode=settings.auth_provider == "local",
    )


@router.post(
    "/dev-token",
    response_model=DevTokenOut,
    summary="Mint a development token (local provider only)",
    dependencies=[Depends(_DEV_TOKEN_LIMIT)],
)
async def dev_token(payload: DevTokenIn, settings: SettingsDep) -> DevTokenOut:
    if settings.auth_provider != "local":
        # Do not advertise a local bypass in managed-IdP deployments.
        raise not_found("Not found.")
    email = str(payload.email).lower()
    subject = f"dev:{email}"
    token = issue_local_token(
        settings,
        subject=subject,
        email=email,
        display_name=payload.display_name,
        org_id=payload.org_id,
        scopes=("service",) if payload.org_id else (),
    )
    return DevTokenOut(access_token=token, expires_in=900)


@router.post(
    "/session",
    response_model=SessionOut,
    status_code=status.HTTP_201_CREATED,
    summary="Create an opaque server-side session",
    dependencies=[Depends(_SESSION_LIMIT)],
)
async def create_session_endpoint(
    payload: SessionCreateIn,
    request: Request,
    response: Response,
    principal: PrincipalDep,
    db: DbDep,
    settings: SettingsDep,
) -> SessionOut:
    ip = client_ip(request, settings)
    session, token = await create_session(
        db,
        principal,
        settings,
        device_label=payload.device_label,
        ip=ip,
        user_agent=request.headers.get("User-Agent"),
    )
    user, profile = await user_service.get_user_with_profile(db, principal)
    await record_event(
        db,
        principal,
        action="auth.session_created",
        resource_type="auth_session",
        resource_id=str(session.id),
        metadata={"device_label": payload.device_label},
        ip=ip,
    )
    # The opaque token is only returned here; the database stores its digest.
    return SessionOut(
        session_id=session.id,
        token=token,
        expires_at=session.expires_at,
        user=_user_out(user, profile),
    )


@router.delete(
    "/session",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Revoke the current session",
)
async def delete_current_session(
    principal: PrincipalDep,
    db: DbDep,
    settings: SettingsDep,
    request: Request,
) -> Response:
    await revoke_current(db, principal)
    await record_event(
        db,
        principal,
        action="auth.signed_out",
        resource_type="auth_session",
        resource_id=str(principal.session_id) if principal.session_id else None,
        ip=client_ip(request, settings),
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/sessions", response_model=list[SessionItem], tags=["auth"])
async def get_sessions(principal: PrincipalDep, db: DbDep) -> list[SessionItem]:
    rows = await list_sessions(db, principal)
    return [
        SessionItem(
            id=row.id,
            device_label=row.device_label,
            ip=str(row.ip) if row.ip is not None else None,
            user_agent=row.user_agent,
            created_at=row.created_at,
            last_used_at=row.last_used_at,
            expires_at=row.expires_at,
            current=row.id == principal.session_id,
        )
        for row in rows
    ]


@router.delete(
    "/sessions/{session_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Revoke one of your sessions",
)
async def delete_session(
    session_id: uuid.UUID,
    principal: PrincipalDep,
    db: DbDep,
    settings: SettingsDep,
    request: Request,
) -> Response:
    revoked = await revoke_session(db, principal, session_id)
    if not revoked:
        raise not_found("Session not found.")
    await record_event(
        db,
        principal,
        action="auth.session_revoked",
        resource_type="auth_session",
        resource_id=str(session_id),
        ip=client_ip(request, settings),
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# Re-exported for router-level dependencies in the app factory.
__all__ = ["router"]
