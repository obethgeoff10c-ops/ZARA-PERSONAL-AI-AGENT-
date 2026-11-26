"""Conversation and message persistence."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from zara.api.pagination import paginate
from zara.auth.principal import Principal
from zara.errors import not_found
from zara.models import Conversation, Message
from zara.services.audit import record_event


async def list_conversations(
    db: AsyncSession,
    principal: Principal,
    *,
    archived: bool = False,
    limit: int = 50,
    cursor: str | None = None,
) -> tuple[list[Conversation], str | None]:
    stmt = select(Conversation).where(
        Conversation.tenant_id == principal.tenant_id,
        Conversation.user_id == principal.user_id,
    )
    stmt = stmt.where(
        Conversation.archived_at.is_not(None) if archived else Conversation.archived_at.is_(None)
    )
    return await paginate(db, stmt, limit=limit, cursor=cursor, model=Conversation)


async def get_conversation(
    db: AsyncSession, principal: Principal, conversation_id: uuid.UUID
) -> Conversation:
    conversation = (
        await db.execute(
            select(Conversation).where(
                Conversation.id == conversation_id,
                Conversation.tenant_id == principal.tenant_id,
                Conversation.user_id == principal.user_id,
            )
        )
    ).scalar_one_or_none()
    if conversation is None:
        raise not_found("Conversation not found.")
    return conversation


async def create_conversation(
    db: AsyncSession,
    principal: Principal,
    *,
    title: str | None,
    ip: str | None = None,
) -> Conversation:
    conversation = Conversation(
        tenant_id=principal.tenant_id,
        user_id=principal.user_id,
        title=title,
        title_source="user" if title else "auto",
    )
    db.add(conversation)
    await db.flush()
    await record_event(
        db,
        principal,
        action="conversation.created",
        resource_type="conversation",
        resource_id=str(conversation.id),
        ip=ip,
    )
    return conversation


async def update_conversation(
    db: AsyncSession,
    principal: Principal,
    conversation_id: uuid.UUID,
    *,
    title: str | None,
    archived: bool | None,
    ip: str | None = None,
) -> Conversation:
    conversation = await get_conversation(db, principal, conversation_id)
    if title is not None:
        conversation.title = title
        conversation.title_source = "user"
    if archived is not None:
        conversation.archived_at = datetime.now(UTC) if archived else None
    await db.flush()
    await record_event(
        db,
        principal,
        action="conversation.updated",
        resource_type="conversation",
        resource_id=str(conversation.id),
        metadata={"archived": archived, "title_changed": title is not None},
        ip=ip,
    )
    return conversation


async def delete_conversation(
    db: AsyncSession,
    principal: Principal,
    conversation_id: uuid.UUID,
    *,
    ip: str | None = None,
) -> None:
    conversation = await get_conversation(db, principal, conversation_id)
    await record_event(
        db,
        principal,
        action="conversation.deleted",
        resource_type="conversation",
        resource_id=str(conversation.id),
        ip=ip,
    )
    await db.delete(conversation)
    await db.flush()


async def list_messages(
    db: AsyncSession,
    principal: Principal,
    conversation_id: uuid.UUID,
    *,
    limit: int = 50,
    cursor: str | None = None,
) -> tuple[list[Message], str | None]:
    await get_conversation(db, principal, conversation_id)
    stmt = select(Message).where(
        Message.conversation_id == conversation_id,
        Message.tenant_id == principal.tenant_id,
        Message.user_id == principal.user_id,
    )
    return await paginate(db, stmt, limit=limit, cursor=cursor, descending=False, model=Message)


async def append_user_message(
    db: AsyncSession,
    principal: Principal,
    conversation_id: uuid.UUID,
    *,
    content: str,
    ip: str | None = None,
) -> Message:
    conversation = await get_conversation(db, principal, conversation_id)
    now = datetime.now(UTC)
    message = Message(
        tenant_id=principal.tenant_id,
        user_id=principal.user_id,
        conversation_id=conversation.id,
        role="user",
        content=content,
        created_at=now,
    )
    db.add(message)
    conversation.last_message_at = now
    await db.flush()
    await record_event(
        db,
        principal,
        action="message.created",
        resource_type="message",
        resource_id=str(message.id),
        metadata={"conversation_id": str(conversation.id)},
        ip=ip,
    )
    return message


async def append_assistant_message(
    db: AsyncSession,
    principal: Principal,
    conversation_id: uuid.UUID,
    *,
    content: str,
    run_id: uuid.UUID | None = None,
    citations: list[dict[str, object]] | None = None,
    content_blocks: list[object] | None = None,
) -> Message:
    conversation = await get_conversation(db, principal, conversation_id)
    now = datetime.now(UTC)
    message = Message(
        tenant_id=principal.tenant_id,
        user_id=principal.user_id,
        conversation_id=conversation.id,
        role="assistant",
        content=content,
        citations=citations,
        content_blocks=content_blocks,
        run_id=run_id,
        created_at=now,
    )
    db.add(message)
    conversation.last_message_at = now
    await db.flush()
    await record_event(
        db,
        principal,
        action="message.created",
        resource_type="message",
        resource_id=str(message.id),
        metadata={"conversation_id": str(conversation.id), "role": "assistant"},
    )
    return message


async def set_message_feedback(
    db: AsyncSession,
    principal: Principal,
    message_id: uuid.UUID,
    *,
    feedback: str | None,
    ip: str | None = None,
) -> Message:
    message = (
        await db.execute(
            select(Message).where(
                Message.id == message_id,
                Message.tenant_id == principal.tenant_id,
                Message.user_id == principal.user_id,
            )
        )
    ).scalar_one_or_none()
    if message is None:
        raise not_found("Message not found.")
    message.feedback = feedback
    await db.flush()
    await record_event(
        db,
        principal,
        action="message.feedback",
        resource_type="message",
        resource_id=str(message.id),
        metadata={"feedback": feedback},
        ip=ip,
    )
    return message


async def unread_message_count(db: AsyncSession, principal: Principal) -> int:
    count = await db.scalar(
        select(func.count(Message.id)).where(
            Message.tenant_id == principal.tenant_id,
            Message.user_id == principal.user_id,
            Message.role == "assistant",
        )
    )
    return int(count or 0)
