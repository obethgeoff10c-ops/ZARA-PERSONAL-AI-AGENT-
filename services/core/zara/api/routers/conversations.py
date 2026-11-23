"""Conversations, messages, and feedback (07-API.md §2)."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request, Response, status

from zara.api.deps import DbDep, PrincipalDep, SettingsDep, UserRateLimit, client_ip
from zara.api.pagination import MAX_PAGE_SIZE
from zara.schemas import (
    ConversationCreate,
    ConversationOut,
    ConversationUpdate,
    FeedbackIn,
    MessageCreate,
    MessageOut,
    Page,
)
from zara.services import conversations as service

router = APIRouter(tags=["conversations"])

_MessageLimit = UserRateLimit("messages", limit=20)


@router.get("/conversations", response_model=Page[ConversationOut], summary="List conversations")
async def list_conversations(
    principal: PrincipalDep,
    db: DbDep,
    archived: bool = False,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = 50,
    cursor: str | None = None,
) -> Page[ConversationOut]:
    rows, next_cursor = await service.list_conversations(
        db, principal, archived=archived, limit=limit, cursor=cursor
    )
    return Page(
        items=[ConversationOut.model_validate(row) for row in rows],
        next_cursor=next_cursor,
    )


@router.post(
    "/conversations",
    response_model=ConversationOut,
    status_code=status.HTTP_201_CREATED,
    summary="Create a conversation",
)
async def create_conversation(
    payload: ConversationCreate,
    principal: PrincipalDep,
    db: DbDep,
    settings: SettingsDep,
    request: Request,
) -> ConversationOut:
    conversation = await service.create_conversation(
        db, principal, title=payload.title, ip=client_ip(request, settings)
    )
    return ConversationOut.model_validate(conversation)


@router.get(
    "/conversations/{conversation_id}",
    response_model=ConversationOut,
    summary="Get a conversation",
)
async def get_conversation(
    conversation_id: uuid.UUID, principal: PrincipalDep, db: DbDep
) -> ConversationOut:
    conversation = await service.get_conversation(db, principal, conversation_id)
    return ConversationOut.model_validate(conversation)


@router.patch(
    "/conversations/{conversation_id}",
    response_model=ConversationOut,
    summary="Update a conversation",
)
async def update_conversation(
    conversation_id: uuid.UUID,
    payload: ConversationUpdate,
    principal: PrincipalDep,
    db: DbDep,
    settings: SettingsDep,
    request: Request,
) -> ConversationOut:
    conversation = await service.update_conversation(
        db,
        principal,
        conversation_id,
        title=payload.title,
        archived=payload.archived,
        ip=client_ip(request, settings),
    )
    return ConversationOut.model_validate(conversation)


@router.delete(
    "/conversations/{conversation_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a conversation and its messages",
)
async def delete_conversation(
    conversation_id: uuid.UUID,
    principal: PrincipalDep,
    db: DbDep,
    settings: SettingsDep,
    request: Request,
) -> Response:
    await service.delete_conversation(
        db, principal, conversation_id, ip=client_ip(request, settings)
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get(
    "/conversations/{conversation_id}/messages",
    response_model=Page[MessageOut],
    summary="List messages (oldest first)",
)
async def list_messages(
    conversation_id: uuid.UUID,
    principal: PrincipalDep,
    db: DbDep,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = 50,
    cursor: str | None = None,
) -> Page[MessageOut]:
    rows, next_cursor = await service.list_messages(
        db, principal, conversation_id, limit=limit, cursor=cursor
    )
    return Page(items=[MessageOut.model_validate(row) for row in rows], next_cursor=next_cursor)


@router.post(
    "/conversations/{conversation_id}/messages",
    response_model=MessageOut,
    status_code=status.HTTP_201_CREATED,
    summary="Append a user message",
    dependencies=[Depends(_MessageLimit)],
)
async def append_message(
    conversation_id: uuid.UUID,
    payload: MessageCreate,
    principal: PrincipalDep,
    db: DbDep,
    settings: SettingsDep,
    request: Request,
) -> MessageOut:
    message = await service.append_user_message(
        db,
        principal,
        conversation_id,
        content=payload.content,
        ip=client_ip(request, settings),
    )
    return MessageOut.model_validate(message)


@router.post(
    "/messages/{message_id}/feedback",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Rate an assistant message",
)
async def message_feedback(
    message_id: uuid.UUID,
    payload: FeedbackIn,
    principal: PrincipalDep,
    db: DbDep,
    settings: SettingsDep,
    request: Request,
) -> Response:
    await service.set_message_feedback(
        db,
        principal,
        message_id,
        feedback=None if payload.feedback == "none" else payload.feedback,
        ip=client_ip(request, settings),
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)
