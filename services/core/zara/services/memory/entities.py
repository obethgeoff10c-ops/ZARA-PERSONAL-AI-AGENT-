"""Entity resolution and relationship persistence for extracted memories.

Turns the lightweight ``EntityCandidate`` objects produced by an extractor into
rows in ``graph.entities``, ``graph.entity_mentions`` and
``graph.entity_relationships``. This is the PostgreSQL relationship layer the
architecture calls for (Person -> company -> meeting -> email -> project ->
task -> deadline -> repository); no separate graph database is introduced.

Resolution is conservative: entities are matched by exact canonical name within
the caller's tenant/user and type. Richer resolution (aliases, identifiers,
embedding similarity) can layer on later without changing the write path.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from zara.auth.principal import Principal
from zara.models import Entity, EntityMention, EntityRelationship, MemoryItem

if TYPE_CHECKING:
    from zara.services.memory.extract import EntityCandidate

_ENTITY_TYPES = {
    "person",
    "company",
    "project",
    "repository",
    "pull_request",
    "issue",
    "release",
    "meeting",
    "deadline",
    "document",
    "topic",
    "place",
    "goal",
}
_RELATION_MENTIONED_WITH = "mentioned_with"


async def resolve_entities(
    db: AsyncSession,
    principal: Principal,
    *,
    memory: MemoryItem,
    candidates: list[EntityCandidate],
    message_id: uuid.UUID | None,
    reference_time: datetime,
) -> list[Entity]:
    """Resolve candidates to entities, record mentions, and link co-mentions."""
    resolved: list[Entity] = []
    seen: set[uuid.UUID] = set()

    for candidate in candidates:
        name = candidate.surface.strip()
        if not name:
            continue
        entity_type = candidate.type if candidate.type in _ENTITY_TYPES else "topic"
        entity = await _find_entity(db, principal, entity_type, name)
        if entity is None:
            entity = Entity(
                tenant_id=principal.tenant_id,
                user_id=principal.user_id,
                type=entity_type,
                canonical_name=name,
                aliases=[],
                identifiers={},
                attributes={},
                confidence=0.7,
                first_seen_at=reference_time,
                last_seen_at=reference_time,
                status="active",
            )
            db.add(entity)
            await db.flush()
        else:
            entity.last_seen_at = reference_time
        if entity.id in seen:
            continue
        seen.add(entity.id)
        resolved.append(entity)
        db.add(
            EntityMention(
                tenant_id=principal.tenant_id,
                entity_id=entity.id,
                message_id=message_id,
                memory_id=memory.id,
                surface_form=name,
                confidence=0.7,
            )
        )

    # Co-mention edges let retrieval traverse from a person to the memory that
    # introduced a related project/task/repository.
    for src in resolved:
        for dst in resolved:
            if src.id == dst.id:
                continue
            await _ensure_relationship(
                db,
                principal,
                src_id=src.id,
                dst_id=dst.id,
                relation=_RELATION_MENTIONED_WITH,
                memory_id=memory.id,
                reference_time=reference_time,
            )

    return resolved


async def _find_entity(
    db: AsyncSession, principal: Principal, entity_type: str, name: str
) -> Entity | None:
    return (
        await db.execute(
            select(Entity).where(
                Entity.tenant_id == principal.tenant_id,
                Entity.user_id == principal.user_id,
                Entity.type == entity_type,
                Entity.status == "active",
                Entity.canonical_name == name,
            )
        )
    ).scalar_one_or_none()


async def _ensure_relationship(
    db: AsyncSession,
    principal: Principal,
    *,
    src_id: uuid.UUID,
    dst_id: uuid.UUID,
    relation: str,
    memory_id: uuid.UUID,
    reference_time: datetime,
) -> None:
    existing = (
        await db.execute(
            select(EntityRelationship.id).where(
                EntityRelationship.tenant_id == principal.tenant_id,
                EntityRelationship.user_id == principal.user_id,
                EntityRelationship.src_entity_id == src_id,
                EntityRelationship.dst_entity_id == dst_id,
                EntityRelationship.relation == relation,
            )
        )
    ).first()
    if existing is not None:
        return
    db.add(
        EntityRelationship(
            tenant_id=principal.tenant_id,
            user_id=principal.user_id,
            src_entity_id=src_id,
            dst_entity_id=dst_id,
            relation=relation,
            weight=1.0,
            confidence=0.7,
            valid_from=reference_time,
            source_memory_id=memory_id,
        )
    )
    await db.flush()
