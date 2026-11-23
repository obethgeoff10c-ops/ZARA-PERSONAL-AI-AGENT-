"""Insights and in-app notifications (02-DATABASE.md §2.10)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from zara.db.base import Base, TenantScopedMixin, TimestampsMixin, UserScopedMixin, UUIDPkMixin


class Insight(UUIDPkMixin, TenantScopedMixin, UserScopedMixin, TimestampsMixin, Base):
    __tablename__ = "insights"
    __table_args__ = (
        CheckConstraint(
            "status IN ('candidate','suppressed','pending_delivery','delivered','dismissed',"
            "'snoozed','acted','expired')",
            name="status",
        ),
        Index("ix_insights_user_status_score", "user_id", "status", text("score DESC")),
        Index(
            "uq_insights_active_dedup",
            "user_id",
            "dedup_key",
            unique=True,
            postgresql_where=text(
                "status IN ('candidate','pending_delivery','delivered','snoozed')"
            ),
        ),
        {"schema": "proactive"},
    )

    detector: Mapped[str] = mapped_column(Text, nullable=False)
    domain: Mapped[list[str] | None] = mapped_column(ARRAY(Text))
    kind: Mapped[str | None] = mapped_column(Text)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    evidence: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    importance: Mapped[float | None] = mapped_column(Float)
    urgency: Mapped[float | None] = mapped_column(Float)
    confidence: Mapped[float | None] = mapped_column(Float)
    novelty: Mapped[float | None] = mapped_column(Float)
    score: Mapped[float] = mapped_column(Float, nullable=False)
    dedup_key: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    suppress_reason: Mapped[str | None] = mapped_column(Text)
    snoozed_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    source_event_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True))
    run_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("agent.agent_runs.id", ondelete="SET NULL")
    )
    proposed_action: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    approval_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("agent.approvals.id", ondelete="SET NULL")
    )


class Notification(UUIDPkMixin, TenantScopedMixin, UserScopedMixin, TimestampsMixin, Base):
    __tablename__ = "notifications"
    __table_args__ = (
        CheckConstraint(
            "kind IN ('insight','approval_request','briefing','system','integration_alert')",
            name="kind",
        ),
        CheckConstraint("channel IN ('in_app','email','web_push')", name="channel"),
        CheckConstraint(
            "priority IS NULL OR priority IN ('low','normal','high','urgent')", name="priority"
        ),
        CheckConstraint(
            "status IS NULL OR status IN "
            "('queued','sent','delivered','read','failed','suppressed')",
            name="status",
        ),
        Index("ix_notifications_user_status_created", "user_id", "status", text("created_at DESC")),
        Index(
            "ix_notifications_user_unread",
            "user_id",
            "read_at",
            postgresql_where=text("read_at IS NULL"),
        ),
        {"schema": "proactive"},
    )

    insight_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("proactive.insights.id", ondelete="SET NULL")
    )
    approval_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("agent.approvals.id", ondelete="SET NULL")
    )
    kind: Mapped[str | None] = mapped_column(Text)
    channel: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[str | None] = mapped_column(Text)
    body: Mapped[str | None] = mapped_column(Text)
    action_url: Mapped[str | None] = mapped_column(Text)
    priority: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str | None] = mapped_column(Text)
    scheduled_for: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    provider_message_id: Mapped[str | None] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(Text)
