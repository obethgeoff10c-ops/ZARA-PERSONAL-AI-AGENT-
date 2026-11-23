"""Integration metadata and lifecycle endpoints (07-API.md §7)."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Request, Response, status

from zara.api.deps import (
    DbDep,
    PrincipalDep,
    SettingsDep,
    UserRateLimit,
    client_ip,
)
from zara.schemas import IntegrationOut, IntegrationSettingsUpdate, Page
from zara.services import integrations as service

router = APIRouter(prefix="/integrations", tags=["integrations"])

_Manage = UserRateLimit("integrations-manage", limit=30)


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
) -> Response:
    await service.revoke(db, principal, integration_id, ip=client_ip(request, settings))
    return Response(status_code=status.HTTP_204_NO_CONTENT)
