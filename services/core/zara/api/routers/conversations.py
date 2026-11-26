"""Conversations, messages, and feedback (07-API.md §2)."""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request, Response, status
from fastapi.responses import StreamingResponse

from zara.agent.engine import AgentEngine
from zara.api.deps import DbDep, PrincipalDep, SettingsDep, UserRateLimit, client_ip
from zara.api.pagination import MAX_PAGE_SIZE
from zara.api.sse import SSE_HEADERS, format_event
from zara.auth.principal import Principal
from zara.config import Settings
from zara.db.session import rls_session
from zara.errors import AppError
from zara.models import RunEvent
from zara.schemas import (
    ChatIn,
    ConversationCreate,
    ConversationOut,
    ConversationUpdate,
    FeedbackIn,
    MessageCreate,
    MessageOut,
    Page,
)
from zara.services import conversations as service
from zara.services import runs as run_service

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


def _engine_for(request: Request) -> AgentEngine:
    engine = getattr(request.app.state, "agent_engine", None)
    if isinstance(engine, AgentEngine):
        return engine
    engine = AgentEngine()
    request.app.state.agent_engine = engine
    return engine


async def _chat_stream(
    *,
    request: Request,
    principal: Principal,
    settings: Settings,
    run_id: uuid.UUID,
) -> AsyncIterator[str]:
    queue: asyncio.Queue[RunEvent | None] = asyncio.Queue()
    final_status = "failed"

    async def sink(event: RunEvent) -> None:
        await queue.put(event)

    async def runner() -> None:
        nonlocal final_status
        try:
            async with rls_session(
                tenant_id=principal.tenant_id, user_id=principal.user_id, role="user"
            ) as session:
                state = await _engine_for(request).run(
                    session,
                    principal,
                    settings,
                    run_id,
                    on_event=sink,
                )
                final_status = state.status.value
        except AppError:
            final_status = "failed"
        except Exception:
            final_status = "failed"
        finally:
            await queue.put(None)

    task = asyncio.create_task(runner())
    try:
        while True:
            event = await queue.get()
            if event is None:
                break
            yield format_event(event=event.type, data=event.data, event_id=event.seq)
        yield format_event(event="done", data={"run_id": str(run_id), "status": final_status})
    finally:
        if not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task


@router.post(
    "/chat",
    summary="Send a chat message and stream the agent response (SSE)",
    dependencies=[Depends(_MessageLimit)],
)
async def chat(
    payload: ChatIn,
    principal: PrincipalDep,
    db: DbDep,
    settings: SettingsDep,
    request: Request,
) -> StreamingResponse:
    if payload.conversation_id is not None:
        conversation = await service.get_conversation(db, principal, payload.conversation_id)
    else:
        conversation = await service.create_conversation(
            db,
            principal,
            title=payload.message[:80],
            ip=client_ip(request, settings),
        )
    message = await service.append_user_message(
        db,
        principal,
        conversation.id,
        content=payload.message,
        ip=client_ip(request, settings),
    )
    run = await run_service.create_run(
        db,
        principal,
        goal=payload.message,
        domains=None,
        conversation_id=conversation.id,
        mode=payload.mode,
        budget=None,
        ip=client_ip(request, settings),
    )
    run.trigger_ref = {
        "user_message_id": str(message.id),
        "client_message_id": (
            str(payload.client_message_id) if payload.client_message_id else None
        ),
        "mode": payload.mode,
    }
    await db.flush()
    # The stream runs in its own RLS transaction; make the run visible first.
    await db.commit()
    return StreamingResponse(
        _chat_stream(
            request=request,
            principal=principal,
            settings=settings,
            run_id=run.id,
        ),
        media_type="text/event-stream",
        headers=SSE_HEADERS,
    )


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
