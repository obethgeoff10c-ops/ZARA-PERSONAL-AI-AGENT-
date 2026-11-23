"""Approval decision endpoints (07-API.md §5)."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Query, Request

from zara.api.deps import DbDep, PrincipalDep, SettingsDep, client_ip
from zara.api.pagination import MAX_PAGE_SIZE
from zara.schemas import (
    ApprovalApproveIn,
    ApprovalEditIn,
    ApprovalOut,
    ApprovalRejectIn,
    Page,
)
from zara.services import approvals as service

router = APIRouter(prefix="/approvals", tags=["approvals"])


@router.get("", response_model=Page[ApprovalOut], summary="List approvals")
async def list_approvals(
    principal: PrincipalDep,
    db: DbDep,
    status_filter: Annotated[str | None, Query(alias="status")] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = 50,
    cursor: str | None = None,
) -> Page[ApprovalOut]:
    rows, next_cursor = await service.list_approvals(
        db, principal, status=status_filter, limit=limit, cursor=cursor
    )
    return Page(items=[ApprovalOut.model_validate(row) for row in rows], next_cursor=next_cursor)


@router.get("/{approval_id}", response_model=ApprovalOut, summary="Approval detail")
async def get_approval(approval_id: uuid.UUID, principal: PrincipalDep, db: DbDep) -> ApprovalOut:
    approval = await service.get_approval(db, principal, approval_id)
    return ApprovalOut.model_validate(approval)


@router.post("/{approval_id}/approve", response_model=ApprovalOut, summary="Approve action")
async def approve(
    approval_id: uuid.UUID,
    payload: ApprovalApproveIn,
    principal: PrincipalDep,
    db: DbDep,
    settings: SettingsDep,
    request: Request,
) -> ApprovalOut:
    approval = await service.approve(
        db,
        principal,
        approval_id,
        args_hash=payload.args_hash,
        note=payload.note,
        ip=client_ip(request, settings),
    )
    return ApprovalOut.model_validate(approval)


@router.post(
    "/{approval_id}/edit-and-approve",
    response_model=ApprovalOut,
    summary="Edit arguments, recompute the hash, and approve",
)
async def edit_and_approve(
    approval_id: uuid.UUID,
    payload: ApprovalEditIn,
    principal: PrincipalDep,
    db: DbDep,
    settings: SettingsDep,
    request: Request,
) -> ApprovalOut:
    approval = await service.edit_and_approve(
        db,
        principal,
        approval_id,
        edits=payload.edits,
        note=payload.note,
        ip=client_ip(request, settings),
    )
    return ApprovalOut.model_validate(approval)


@router.post("/{approval_id}/reject", response_model=ApprovalOut, summary="Reject action")
async def reject(
    approval_id: uuid.UUID,
    payload: ApprovalRejectIn,
    principal: PrincipalDep,
    db: DbDep,
    settings: SettingsDep,
    request: Request,
) -> ApprovalOut:
    approval = await service.reject(
        db,
        principal,
        approval_id,
        reason=payload.reason,
        ip=client_ip(request, settings),
    )
    return ApprovalOut.model_validate(approval)
