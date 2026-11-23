"""Entities and relationships used by memory, projects, and contacts."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    CheckConstraint,
    Computed,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, TSVECTOR
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from zara.db.base import Base, TenantScopedMixin, TimestampsMixin, UserScopedMixin, UUIDPkMixin


class Entity(UUIDPkMixin, TenantScopedMixin, UserScopedMixin, TimestampsMixin, Base):
    __tablename__ = "entities"
    __table_args__ = (
        CheckConstraint(
            "type IN ('person','company','project','repository','pull_request','issue',"
            "'release','meeting','deadline','document','topic','place','goal')",
            name="type",
        ),
        CheckConstraint("status IN ('active','merged','archived')", name="status"),
        Index("ix_entities_user_type_status", "user_id", "type", "status"),
        Index(
            "ix_entities_identifiers",
            "identifiers",
            postgresql_using="gin",
            postgresql_ops={"identifiers": "jsonb_path_ops"},
        ),
        Index("ix_entities_aliases", "aliases", postgresql_using="gin"),
        Index(
            "ix_entities_canonical_name_trgm",
            "canonical_name",
            postgresql_using="gin",
            postgresql_ops={"canonical_name": "gin_trgm_ops"},
        ),
        Index(
            "ix_entities_embedding_hnsw",
            "embedding",
            postgresql_using="hnsw",
            postgresql_with={"m": 16, "ef_construction": 64},
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
        Index(
            "uq_entities_primary_key",
            "user_id",
            "type",
            text("(identifiers->>'primary_key')"),
            unique=True,
            postgresql_where=text("status = 'active' AND identifiers ? 'primary_key'"),
        ),
        {"schema": "graph"},
    )

    type: Mapped[str] = mapped_column(Text, nullable=False)
    canonical_name: Mapped[str] = mapped_column(Text, nullable=False)
    aliases: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, server_default=text("'{}'::text[]")
    )
    identifiers: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    attributes: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    embedding: Mapped[list[float] | None] = mapped_column(Vector(1024))
    merged_into_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("graph.entities.id", ondelete="SET NULL")
    )
    confidence: Mapped[float] = mapped_column(Float, nullable=False, server_default=text("1.0"))
    first_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'active'"))
    tsv: Mapped[str | None] = mapped_column(
        TSVECTOR,
        Computed(
            "to_tsvector('english'::regconfig, canonical_name)",
            persisted=True,
        ),
    )


class EntityRelationship(UUIDPkMixin, TenantScopedMixin, UserScopedMixin, TimestampsMixin, Base):
    __tablename__ = "entity_relationships"
    __table_args__ = (
        CheckConstraint("src_entity_id <> dst_entity_id", name="distinct_endpoints"),
        Index("ix_entity_relationships_src", "src_entity_id", "relation"),
        Index("ix_entity_relationships_dst", "dst_entity_id", "relation"),
        Index(
            "uq_entity_relationships_edge",
            "src_entity_id",
            "dst_entity_id",
            "relation",
            text("coalesce(valid_from, '-infinity'::timestamptz)"),
            unique=True,
        ),
        {"schema": "graph"},
    )

    src_entity_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("graph.entities.id", ondelete="CASCADE"), nullable=False
    )
    dst_entity_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("graph.entities.id", ondelete="CASCADE"), nullable=False
    )
    relation: Mapped[str] = mapped_column(Text, nullable=False)
    weight: Mapped[float] = mapped_column(Float, nullable=False, server_default=text("1.0"))
    confidence: Mapped[float] = mapped_column(Float, nullable=False, server_default=text("1.0"))
    valid_from: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    valid_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    source_memory_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("memory.memory_items.id", ondelete="SET NULL")
    )
    source_document_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True))


class EntityMention(UUIDPkMixin, TenantScopedMixin, Base):
    __tablename__ = "entity_mentions"
    __table_args__ = (
        Index("ix_entity_mentions_entity_id", "entity_id"),
        Index("ix_entity_mentions_document_id", "document_id"),
        {"schema": "graph"},
    )

    entity_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("graph.entities.id", ondelete="CASCADE"), nullable=False
    )
    document_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True))
    message_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("agent.messages.id", ondelete="SET NULL")
    )
    memory_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("memory.memory_items.id", ondelete="CASCADE")
    )
    surface_form: Mapped[str | None] = mapped_column(Text)
    confidence: Mapped[float | None] = mapped_column(Float)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
