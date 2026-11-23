"""Memory records, provenance, embeddings, and revisions (02-DATABASE.md §2.4)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Computed,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    SmallInteger,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, TSVECTOR
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from zara.db.base import Base, TenantScopedMixin, TimestampsMixin, UserScopedMixin, UUIDPkMixin


class MemoryItem(UUIDPkMixin, TenantScopedMixin, UserScopedMixin, TimestampsMixin, Base):
    __tablename__ = "memory_items"
    __table_args__ = (
        CheckConstraint(
            "kind IN ('episodic','semantic','preference','goal','relationship','project',"
            "'commitment','procedural')",
            name="kind",
        ),
        CheckConstraint("importance >= 0 AND importance <= 1", name="importance_range"),
        CheckConstraint("confidence >= 0 AND confidence <= 1", name="confidence_range"),
        CheckConstraint("sensitivity IN ('normal','sensitive','restricted')", name="sensitivity"),
        CheckConstraint("trust_level IN ('user','zara','external_untrusted')", name="trust_level"),
        CheckConstraint(
            "status IN ('suggested','active','superseded','archived','expired','deleted')",
            name="status",
        ),
        CheckConstraint(
            "created_by IN ('extractor','user','consolidation','system')", name="created_by"
        ),
        CheckConstraint(
            "NOT (trust_level = 'external_untrusted' AND kind IN ('procedural','preference'))",
            name="trust_gate",
        ),
        CheckConstraint(
            "valid_to IS NULL OR valid_from IS NULL OR valid_to >= valid_from",
            name="valid_window",
        ),
        CheckConstraint("status <> 'active' OR confidence >= 0.0", name="active_confidence"),
        Index("ix_memory_items_user_kind_status", "user_id", "kind", "status"),
        Index(
            "ix_memory_items_user_canonical_key",
            "user_id",
            "canonical_key",
            postgresql_where=text("status = 'active'"),
        ),
        Index(
            "ix_memory_items_user_due_at",
            "user_id",
            "due_at",
            postgresql_where=text("kind = 'commitment' AND status = 'active'"),
        ),
        Index(
            "ix_memory_items_user_expires_at",
            "user_id",
            "expires_at",
            postgresql_where=text("expires_at IS NOT NULL"),
        ),
        Index("ix_memory_items_tsv", "tsv", postgresql_using="gin"),
        Index("ix_memory_items_user_last_accessed", "user_id", "last_accessed_at"),
        {"schema": "memory"},
    )

    kind: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[str | None] = mapped_column(Text)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    importance: Mapped[float] = mapped_column(Float, nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    sensitivity: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'normal'"))
    trust_level: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'active'"))
    user_pinned: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    user_edited: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    valid_from: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    valid_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    occurred_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    supersedes_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("memory.memory_items.id", ondelete="SET NULL")
    )
    canonical_key: Mapped[str | None] = mapped_column(Text)
    content_hash: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    access_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    last_accessed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    decay_score: Mapped[float] = mapped_column(Float, nullable=False, server_default=text("1.0"))
    tsv: Mapped[str | None] = mapped_column(
        TSVECTOR,
        Computed(
            "to_tsvector('english'::regconfig, coalesce(title, '') || ' ' || content)",
            persisted=True,
        ),
    )
    created_by: Mapped[str] = mapped_column(Text, nullable=False)
    extraction_run_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True))
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))

    __mapper_args__ = {"version_id_col": version}  # noqa: RUF012


class MemorySource(UUIDPkMixin, TenantScopedMixin, Base):
    __tablename__ = "memory_sources"
    __table_args__ = (
        CheckConstraint(
            "source_type IN ('document','message','agent_run','user_edit','event')",
            name="source_type",
        ),
        CheckConstraint("quote IS NULL OR length(quote) <= 300", name="quote_length"),
        Index("ix_memory_sources_memory_id", "memory_id"),
        Index("ix_memory_sources_document_id", "document_id"),
        {"schema": "memory"},
    )

    memory_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("memory.memory_items.id", ondelete="CASCADE"),
        nullable=False,
    )
    source_type: Mapped[str] = mapped_column(Text, nullable=False)
    document_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True))
    message_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("agent.messages.id", ondelete="SET NULL")
    )
    run_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("agent.agent_runs.id", ondelete="SET NULL")
    )
    event_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True))
    span_start: Mapped[int | None] = mapped_column(Integer)
    span_end: Mapped[int | None] = mapped_column(Integer)
    quote: Mapped[str | None] = mapped_column(Text)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


class MemoryEmbedding(UUIDPkMixin, TenantScopedMixin, UserScopedMixin, Base):
    __tablename__ = "memory_embeddings"
    __table_args__ = (
        Index(
            "uq_memory_embeddings_current",
            "memory_id",
            unique=True,
            postgresql_where=text("is_current"),
        ),
        Index(
            "ix_memory_embeddings_hnsw",
            "embedding",
            postgresql_using="hnsw",
            postgresql_with={"m": 16, "ef_construction": 64},
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
        {"schema": "memory"},
    )

    memory_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("memory.memory_items.id", ondelete="CASCADE"),
        nullable=False,
    )
    embedding: Mapped[list[float]] = mapped_column(Vector(1024), nullable=False)
    embedding_model: Mapped[str] = mapped_column(Text, nullable=False)
    embedding_version: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    is_current: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


class MemoryRevision(UUIDPkMixin, TenantScopedMixin, Base):
    __tablename__ = "memory_revisions"
    __table_args__ = (
        UniqueConstraint("memory_id", "revision", name="uq_memory_revisions_memory_revision"),
        {"schema": "memory"},
    )

    memory_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("memory.memory_items.id", ondelete="CASCADE"),
        nullable=False,
    )
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    content: Mapped[str | None] = mapped_column(Text)
    payload: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    changed_by: Mapped[str | None] = mapped_column(Text)
    change_reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


class RetrievalLog(UUIDPkMixin, TenantScopedMixin, UserScopedMixin, Base):
    __tablename__ = "retrieval_logs"
    __table_args__ = (
        Index("ix_retrieval_logs_user_created", "user_id", "created_at"),
        {"schema": "memory"},
    )

    run_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("agent.agent_runs.id", ondelete="SET NULL")
    )
    query_text_hash: Mapped[bytes | None] = mapped_column(LargeBinary)
    query_redacted: Mapped[str | None] = mapped_column(Text)
    domains: Mapped[list[str] | None] = mapped_column(ARRAY(Text))
    candidates: Mapped[Any] = mapped_column(JSONB)
    selected_ids: Mapped[list[uuid.UUID] | None] = mapped_column(ARRAY(PGUUID(as_uuid=True)))
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
