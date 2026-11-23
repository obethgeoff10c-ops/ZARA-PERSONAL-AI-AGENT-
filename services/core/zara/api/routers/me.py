"""Current-user profile and preference endpoints (07-API.md §1)."""

from __future__ import annotations

from fastapi import APIRouter, Request

from zara.api.deps import DbDep, PrincipalDep, SettingsDep, client_ip
from zara.schemas import (
    PermissionsOut,
    PreferencesOut,
    PreferencesUpdate,
    ProfileOut,
    ProfileUpdate,
    UserOut,
)
from zara.services import users as user_service

router = APIRouter(prefix="/me", tags=["identity"])


@router.get("", response_model=UserOut, summary="Current user")
async def get_me(principal: PrincipalDep, db: DbDep) -> UserOut:
    user, profile = await user_service.get_user_with_profile(db, principal)
    return UserOut(
        id=user.id,
        tenant_id=user.tenant_id,
        email=user.email,
        display_name=user.display_name,
        role=user.role,
        status=user.status,
        created_at=user.created_at,
        profile=profile,
    )


@router.patch("/profile", response_model=ProfileOut, summary="Update your profile")
async def patch_profile(
    payload: ProfileUpdate,
    principal: PrincipalDep,
    db: DbDep,
    settings: SettingsDep,
    request: Request,
) -> ProfileOut:
    changes = payload.model_dump(exclude_unset=True)
    profile = await user_service.update_profile(
        db, principal, changes, ip=client_ip(request, settings)
    )
    return ProfileOut.model_validate(profile)


@router.get(
    "/preferences/{namespace}",
    response_model=PreferencesOut,
    summary="Read a preference namespace",
)
async def get_preferences(namespace: str, principal: PrincipalDep, db: DbDep) -> PreferencesOut:
    values = await user_service.get_preferences(db, principal, namespace)
    return PreferencesOut(namespace=namespace, values=values)


@router.put(
    "/preferences/{namespace}",
    response_model=PreferencesOut,
    summary="Replace a preference namespace",
)
async def put_preferences(
    namespace: str,
    payload: PreferencesUpdate,
    principal: PrincipalDep,
    db: DbDep,
    settings: SettingsDep,
    request: Request,
) -> PreferencesOut:
    values = await user_service.put_preferences(
        db,
        principal,
        namespace,
        payload.values,
        ip=client_ip(request, settings),
    )
    return PreferencesOut(namespace=namespace, values=values)


@router.get(
    "/permissions",
    response_model=PermissionsOut,
    summary="Your effective permissions",
)
async def get_permissions(principal: PrincipalDep) -> PermissionsOut:
    return PermissionsOut(
        role=principal.role,
        permissions=user_service.permission_list(principal),
    )
