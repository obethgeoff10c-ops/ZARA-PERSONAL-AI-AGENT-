"""CRM contacts (the subset of §2.8 needed by tasks and projects)."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, CITEXT
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from zara.db.base import Base, TenantScopedMixin, TimestampsMixin, UserScopedMixin, UUIDPkMixin


class Contact(UUIDPkMixin, TenantScopedMixin, UserScopedMixin, TimestampsMixin, Base):
    __tablename__ = "contacts"
    __table_args__ = (
        CheckConstraint(
            "relationship IS NULL OR relationship IN ('prospect','customer','partner','investor',"
            "'colleague','friend','family','other')",
            name="relationship",
        ),
        Index("ix_contacts_emails", "emails", postgresql_using="gin"),
        Index(
            "ix_contacts_full_name_trgm",
            "full_name",
            postgresql_using="gin",
            postgresql_ops={"full_name": "gin_trgm_ops"},
        ),
        Index("ix_contacts_user_next_followup", "user_id", "next_followup_at"),
        {"schema": "crm"},
    )

    entity_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("graph.entities.id", ondelete="SET NULL"),
        unique=True,
    )
    full_name: Mapped[str] = mapped_column(Text, nullable=False)
    emails: Mapped[list[str]] = mapped_column(
        ARRAY(CITEXT), nullable=False, server_default=text("'{}'::citext[]")
    )
    phones: Mapped[list[str] | None] = mapped_column(ARRAY(Text))
    title: Mapped[str | None] = mapped_column(Text)
    company_entity_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("graph.entities.id", ondelete="SET NULL")
    )
    company_name: Mapped[str | None] = mapped_column(Text)
    linkedin_url: Mapped[str | None] = mapped_column(Text)
    relationship: Mapped[str | None] = mapped_column(Text)
    last_interaction_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_followup_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    notes_memory_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("memory.memory_items.id", ondelete="SET NULL")
    )
    tags: Mapped[list[str] | None] = mapped_column(ARRAY(Text))
