"""Memory and provenance persistence (CRUD layer)."""

from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from zara.api.pagination import paginate
from zara.auth.principal import Principal
from zara.errors import conflict, not_found, precondition_failed, validation_failed
from zara.models import BlockedFact, MemoryEmbedding, MemoryItem, MemoryRevision, MemorySource
from zara.services.audit import record_event
from zara.services.memory.embeddings import store_current_embedding
from zara.services.memory.keys import derive_canonical_key

_ALLOWED_KINDS = {
    "episodic",
    "semantic",
    "preference",
    "goal",
    "relationship",
    "project",
    "commitment",
    "procedural",
}


def _hash_content(content: str) -> bytes:
    return hashlib.sha256(content.encode("utf-8")).digest()


async def list_memory(
    db: AsyncSession,
    principal: Principal,
    *,
    q: str | None = None,
    kind: str | None = None,
    status: str | None = "active",
    pinned: bool | None = None,
    limit: int = 50,
    cursor: str | None = None,
) -> tuple[list[MemoryItem], str | None]:
    stmt = select(MemoryItem).where(
        MemoryItem.tenant_id == principal.tenant_id,
        MemoryItem.user_id == principal.user_id,
    )
    if status:
        stmt = stmt.where(MemoryItem.status == status)
    if kind:
        stmt = stmt.where(MemoryItem.kind == kind)
    if pinned is not None:
        stmt = stmt.where(MemoryItem.user_pinned.is_(pinned))
    if q:
        pattern = f"%{q}%"
        stmt = stmt.where(
            or_(
                MemoryItem.title.ilike(pattern),
                MemoryItem.content.ilike(pattern),
                MemoryItem.tsv.op("@@")(func.plainto_tsquery("english", q)),
            )
        )
    return await paginate(db, stmt, limit=limit, cursor=cursor, model=MemoryItem)


async def get_memory(db: AsyncSession, principal: Principal, memory_id: uuid.UUID) -> MemoryItem:
    memory = (
        await db.execute(
            select(MemoryItem).where(
                MemoryItem.id == memory_id,
                MemoryItem.tenant_id == principal.tenant_id,
                MemoryItem.user_id == principal.user_id,
            )
        )
    ).scalar_one_or_none()
    if memory is None:
        raise not_found("Memory item not found.")
    return memory


async def list_sources(
    db: AsyncSession, principal: Principal, memory_id: uuid.UUID
) -> list[MemorySource]:
    result = await db.execute(
        select(MemorySource)
        .where(
            MemorySource.memory_id == memory_id,
            MemorySource.tenant_id == principal.tenant_id,
        )
        .order_by(MemorySource.observed_at.desc())
    )
    return list(result.scalars())


async def create_memory(
    db: AsyncSession,
    principal: Principal,
    values: dict[str, Any],
    *,
    ip: str | None = None,
) -> MemoryItem:
    kind = values.get("kind", "")
    if kind not in _ALLOWED_KINDS:
        raise validation_failed(
            "Unknown memory kind.",
            errors=[{"field": "kind", "message": f"Expected one of {sorted(_ALLOWED_KINDS)}"}],
        )
    now = datetime.now(UTC)
    memory = MemoryItem(
        tenant_id=principal.tenant_id,
        user_id=principal.user_id,
        kind=kind,
        title=values.get("title"),
        content=values["content"],
        payload=values.get("payload") or {},
        importance=values.get("importance", 0.5),
        confidence=values.get("confidence", 1.0),
        sensitivity=values.get("sensitivity", "normal"),
        trust_level="user",
        status="active",
        user_pinned=bool(values.get("pin", False)),
        valid_from=now,
        expires_at=values.get("expires_at"),
        due_at=values.get("due_at"),
        canonical_key=values.get("canonical_key"),
        content_hash=_hash_content(values["content"]),
        created_by="user",
    )
    db.add(memory)
    await db.flush()
    await store_current_embedding(
        db,
        tenant_id=principal.tenant_id,
        user_id=principal.user_id,
        memory_id=memory.id,
        text=f"{memory.title or ''} {memory.content}".strip(),
    )
    await record_event(
        db,
        principal,
        action="memory.created",
        resource_type="memory_item",
        resource_id=str(memory.id),
        metadata={"kind": memory.kind},
        ip=ip,
    )
    return memory


async def update_memory(
    db: AsyncSession,
    principal: Principal,
    memory_id: uuid.UUID,
    changes: dict[str, Any],
    *,
    if_match_version: int | None = None,
    ip: str | None = None,
) -> MemoryItem:
    memory = await get_memory(db, principal, memory_id)
    if if_match_version is not None and memory.version != if_match_version:
        raise precondition_failed(
            "The memory item was modified by another request; reload and retry."
        )
    if memory.status in {"deleted", "superseded"}:
        raise conflict("This memory item is no longer editable.")
    content_changed = "content" in changes
    if content_changed:
        new_content = changes.pop("content")
        memory.content = new_content
        memory.content_hash = _hash_content(new_content)
        new_key = derive_canonical_key(new_content)
        clash = (
            await db.execute(
                select(MemoryItem.id).where(
                    MemoryItem.tenant_id == principal.tenant_id,
                    MemoryItem.user_id == principal.user_id,
                    MemoryItem.canonical_key == new_key,
                    MemoryItem.id != memory.id,
                    MemoryItem.status == "active",
                )
            )
        ).first()
        memory.canonical_key = None if clash is not None else new_key
    if "pinned" in changes:
        memory.user_pinned = bool(changes.pop("pinned"))
    if "payload" in changes:
        memory.payload = changes.pop("payload")
    if "title" in changes:
        memory.title = changes.pop("title")
    if "importance" in changes:
        memory.importance = changes.pop("importance")
    if "expires_at" in changes:
        memory.expires_at = changes.pop("expires_at")
    memory.user_edited = True
    if content_changed:
        await snapshot_revision(db, memory)
    await db.flush()
    if content_changed:
        await store_current_embedding(
            db,
            tenant_id=principal.tenant_id,
            user_id=principal.user_id,
            memory_id=memory.id,
            text=f"{memory.title or ''} {memory.content}".strip(),
        )
    await record_event(
        db,
        principal,
        action="memory.updated",
        resource_type="memory_item",
        resource_id=str(memory.id),
        metadata={"version": memory.version},
        ip=ip,
    )
    return memory


async def delete_memory(
    db: AsyncSession,
    principal: Principal,
    memory_id: uuid.UUID,
    *,
    block: bool = False,
    ip: str | None = None,
) -> None:
    memory = await get_memory(db, principal, memory_id)
    if block:
        # Negative memory: remember the hash so a later sync cannot re-create
        # the fact, and drop the embedding immediately (03 §7).
        existing = (
            await db.execute(
                select(BlockedFact.id).where(
                    BlockedFact.tenant_id == principal.tenant_id,
                    BlockedFact.user_id == principal.user_id,
                    BlockedFact.fact_hash == memory.content_hash,
                )
            )
        ).first()
        if existing is None:
            db.add(
                BlockedFact(
                    tenant_id=principal.tenant_id,
                    user_id=principal.user_id,
                    fact_hash=memory.content_hash,
                    canonical_key=memory.canonical_key,
                    reason="deleted",
                )
            )
        await db.execute(
            delete(MemoryEmbedding).where(
                MemoryEmbedding.memory_id == memory.id,
                MemoryEmbedding.tenant_id == principal.tenant_id,
            )
        )
        memory.status = "deleted"
        await db.flush()
    else:
        await db.delete(memory)
        await db.flush()
    await record_event(
        db,
        principal,
        action="memory.deleted",
        resource_type="memory_item",
        resource_id=str(memory_id),
        metadata={"blocked": block},
        ip=ip,
    )


async def confirm_memory(
    db: AsyncSession,
    principal: Principal,
    memory_id: uuid.UUID,
    *,
    ip: str | None = None,
) -> MemoryItem:
    memory = await get_memory(db, principal, memory_id)
    if memory.status != "suggested":
        raise conflict("Only suggested memories can be confirmed.")
    if memory.canonical_key is not None:
        clash = (
            await db.execute(
                select(MemoryItem.id).where(
                    MemoryItem.tenant_id == principal.tenant_id,
                    MemoryItem.user_id == principal.user_id,
                    MemoryItem.canonical_key == memory.canonical_key,
                    MemoryItem.id != memory.id,
                    MemoryItem.status == "active",
                )
            )
        ).first()
        if clash is not None:
            memory.canonical_key = None
    memory.status = "active"
    memory.confidence = max(memory.confidence, 0.9)
    await db.flush()
    await record_event(
        db,
        principal,
        action="memory.confirmed",
        resource_type="memory_item",
        resource_id=str(memory.id),
        ip=ip,
    )
    return memory


async def reject_memory(
    db: AsyncSession,
    principal: Principal,
    memory_id: uuid.UUID,
    *,
    ip: str | None = None,
) -> None:
    memory = await get_memory(db, principal, memory_id)
    if memory.status not in {"suggested", "active"}:
        raise conflict("This memory cannot be rejected in its current state.")
    memory.status = "deleted"
    await db.flush()
    await record_event(
        db,
        principal,
        action="memory.rejected",
        resource_type="memory_item",
        resource_id=str(memory.id),
        ip=ip,
    )


async def list_suggestions(
    db: AsyncSession, principal: Principal, *, limit: int = 50
) -> list[MemoryItem]:
    result = await db.execute(
        select(MemoryItem)
        .where(
            MemoryItem.tenant_id == principal.tenant_id,
            MemoryItem.user_id == principal.user_id,
            MemoryItem.status == "suggested",
        )
        .order_by(MemoryItem.created_at.desc())
        .limit(min(limit, 100))
    )
    return list(result.scalars())


async def search_memory(
    db: AsyncSession,
    principal: Principal,
    *,
    query: str,
    kinds: list[str] | None = None,
    limit: int = 20,
) -> list[MemoryItem]:
    pattern = f"%{query}%"
    stmt = select(MemoryItem).where(
        MemoryItem.tenant_id == principal.tenant_id,
        MemoryItem.user_id == principal.user_id,
        MemoryItem.status == "active",
        or_(
            MemoryItem.title.ilike(pattern),
            MemoryItem.content.ilike(pattern),
            MemoryItem.tsv.op("@@")(func.plainto_tsquery("english", query)),
        ),
    )
    if kinds:
        stmt = stmt.where(MemoryItem.kind.in_(kinds))
    result = await db.execute(stmt.order_by(MemoryItem.importance.desc()).limit(min(limit, 50)))
    return list(result.scalars())


async def snapshot_revision(db: AsyncSession, memory: MemoryItem) -> None:
    """Record the current state when a material edit happens."""
    revision = (
        await db.scalar(
            select(func.max(MemoryRevision.revision)).where(MemoryRevision.memory_id == memory.id)
        )
        or 0
    )
    db.add(
        MemoryRevision(
            tenant_id=memory.tenant_id,
            memory_id=memory.id,
            revision=revision + 1,
            content=memory.content,
            payload=memory.payload,
            changed_by="user",
            change_reason="memory_edit",
        )
    )
