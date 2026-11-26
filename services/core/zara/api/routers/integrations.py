"""Integration metadata and lifecycle endpoints (07-API.md §7)."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Annotated, Literal
from urllib.parse import urlencode, urlsplit, urlunsplit

import httpx
from fastapi import APIRouter, Depends, Request, Response, status
from fastapi.responses import RedirectResponse

from zara.api.deps import (
    DbDep,
    PrincipalDep,
    SettingsDep,
    UserRateLimit,
    client_ip,
)
from zara.auth.principal import Principal
from zara.db.session import rls_session
from zara.schemas import (
    IntegrationConnectIn,
    IntegrationConnectOut,
    IntegrationOut,
    IntegrationSettingsUpdate,
    Page,
)
from zara.services import integrations as service
from zara.services import oauth

router = APIRouter(prefix="/integrations", tags=["integrations"])

_Manage = UserRateLimit("integrations-manage", limit=30)
_Connect = UserRateLimit("integration-connect", limit=10, window_seconds=3600)

ProviderName = Literal["gmail", "gcal", "gdrive", "github"]


async def get_oauth_http_client(request: Request) -> AsyncIterator[httpx.AsyncClient]:
    """Reuse an injected client/transport in tests; otherwise open one per call."""
    client = getattr(request.app.state, "oauth_http_client", None)
    if isinstance(client, httpx.AsyncClient):
        yield client
        return
    transport = getattr(request.app.state, "oauth_transport", None)
    async with httpx.AsyncClient(timeout=httpx.Timeout(15.0), transport=transport) as created:
        yield created


HttpClientDep = Annotated[httpx.AsyncClient, Depends(get_oauth_http_client)]


@router.post(
    "/{provider}/connect",
    response_model=IntegrationConnectOut,
    summary="Start an OAuth connection (authorization URL + state)",
    dependencies=[Depends(_Connect)],
)
async def connect(
    provider: ProviderName,
    payload: IntegrationConnectIn,
    principal: PrincipalDep,
    db: DbDep,
    settings: SettingsDep,
) -> IntegrationConnectOut:
    authorization = await oauth.begin_connect(
        db,
        principal,
        settings,
        provider=provider,
        scopes=payload.scopes,
        redirect_after=payload.redirect_after,
    )
    return IntegrationConnectOut(
        authorization_url=authorization.authorization_url,
        state=authorization.state,
        expires_at=authorization.expires_at,
    )


@router.get(
    "/{provider}/callback",
    summary="OAuth redirect callback (state-bound, no bearer header)",
)
async def callback(
    provider: ProviderName,
    code: str,
    state: str,
    settings: SettingsDep,
    client: HttpClientDep,
) -> RedirectResponse:
    # The signed state carries the tenant/user so the browser callback can open
    # the correct RLS-scoped transaction without an Authorization header.
    _, tenant_id, user_id = oauth.decode_state(settings, state)
    principal = Principal(
        tenant_id=tenant_id,
        user_id=user_id,
        role="user",
        email="",
        display_name=None,
        auth_provider="oauth",
        external_auth_id="",
    )
    async with rls_session(tenant_id=tenant_id, user_id=user_id, role="user") as db:
        connected = await oauth.complete_connect(
            db,
            principal,
            settings,
            provider=provider,
            code=code,
            state=state,
            client=client,
        )
        provider_name = connected.integration.provider

    parts = urlsplit(str(settings.oauth_frontend_redirect_url))
    query = dict(item.split("=", 1) for item in parts.query.split("&") if item and "=" in item)
    query["connected"] = provider_name
    target = urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))
    return RedirectResponse(target, status_code=status.HTTP_302_FOUND)


@router.get("", response_model=Page[IntegrationOut], summary="List integrations")
async def list_integrations(principal: PrincipalDep, db: DbDep) -> Page[IntegrationOut]:
    rows = await service.list_integrations(db, principal)
    return Page(items=[IntegrationOut.model_validate(row) for row in rows])


@router.get("/{integration_id}", response_model=IntegrationOut, summary="Get an integration")
async def get_integration(
    integration_id: uuid.UUID, principal: PrincipalDep, db: DbDep
) -> IntegrationOut:
    return IntegrationOut.model_validate(
        await service.get_integration(db, principal, integration_id)
    )


@router.patch(
    "/{integration_id}/settings",
    response_model=IntegrationOut,
    summary="Update provider settings",
    dependencies=[Depends(_Manage)],
)
async def update_settings(
    integration_id: uuid.UUID,
    payload: IntegrationSettingsUpdate,
    principal: PrincipalDep,
    db: DbDep,
    settings: SettingsDep,
    request: Request,
) -> IntegrationOut:
    integration = await service.update_settings(
        db,
        principal,
        integration_id,
        payload.settings,
        ip=client_ip(request, settings),
    )
    return IntegrationOut.model_validate(integration)


@router.post(
    "/{integration_id}/pause",
    response_model=IntegrationOut,
    summary="Pause an integration",
    dependencies=[Depends(_Manage)],
)
async def pause_integration(
    integration_id: uuid.UUID,
    principal: PrincipalDep,
    db: DbDep,
    settings: SettingsDep,
    request: Request,
) -> IntegrationOut:
    integration = await service.set_paused(
        db, principal, integration_id, paused=True, ip=client_ip(request, settings)
    )
    return IntegrationOut.model_validate(integration)


@router.post(
    "/{integration_id}/resume",
    response_model=IntegrationOut,
    summary="Resume a paused integration",
    dependencies=[Depends(_Manage)],
)
async def resume_integration(
    integration_id: uuid.UUID,
    principal: PrincipalDep,
    db: DbDep,
    settings: SettingsDep,
    request: Request,
) -> IntegrationOut:
    integration = await service.set_paused(
        db, principal, integration_id, paused=False, ip=client_ip(request, settings)
    )
    return IntegrationOut.model_validate(integration)


@router.delete(
    "/{integration_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Revoke an integration and delete its credentials",
    dependencies=[Depends(_Manage)],
)
async def revoke_integration(
    integration_id: uuid.UUID,
    principal: PrincipalDep,
    db: DbDep,
    settings: SettingsDep,
    request: Request,
    client: HttpClientDep,
) -> Response:
    await service.revoke(
        db,
        principal,
        integration_id,
        settings=settings,
        http_client=client,
        ip=client_ip(request, settings),
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)
