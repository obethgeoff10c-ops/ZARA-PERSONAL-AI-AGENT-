"""Projects, goals, and tasks (02-DATABASE.md §2.7)."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    SmallInteger,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from zara.db.base import Base, TenantScopedMixin, TimestampsMixin, UserScopedMixin, UUIDPkMixin


class Project(UUIDPkMixin, TenantScopedMixin, UserScopedMixin, TimestampsMixin, Base):
    __tablename__ = "projects"
    __table_args__ = (
        CheckConstraint(
            "status IN ('planned','active','on_hold','completed','archived')", name="status"
        ),
        CheckConstraint("priority >= 1 AND priority <= 5", name="priority_range"),
        Index("ix_projects_user_status", "user_id", "status"),
        {"schema": "work"},
    )

    entity_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("graph.entities.id", ondelete="SET NULL"),
        unique=True,
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    priority: Mapped[int | None] = mapped_column(SmallInteger, server_default=text("3"))
    start_date: Mapped[date | None] = mapped_column(Date)
    target_date: Mapped[date | None] = mapped_column(Date)
    owner_contact_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("crm.contacts.id", ondelete="SET NULL")
    )
    repository_ids: Mapped[list[uuid.UUID] | None] = mapped_column(ARRAY(PGUUID(as_uuid=True)))
    metadata_json: Mapped[dict[str, Any] | None] = mapped_column("metadata", JSONB)


class Goal(UUIDPkMixin, TenantScopedMixin, UserScopedMixin, TimestampsMixin, Base):
    __tablename__ = "goals"
    __table_args__ = (
        CheckConstraint("domain IN ('personal','executive','sales','developer')", name="domain"),
        CheckConstraint("status IN ('active','achieved','abandoned','paused')", name="status"),
        Index("ix_goals_user_status", "user_id", "status"),
        Index(
            "ix_goals_user_target_date",
            "user_id",
            "target_date",
            postgresql_where=text("status = 'active'"),
        ),
        {"schema": "work"},
    )

    title: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    domain: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str | None] = mapped_column(Text, server_default=text("'active'"))
    target_metric: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    current_value: Mapped[float | None] = mapped_column(Numeric)
    progress_pct: Mapped[float | None] = mapped_column(Numeric(5, 2))
    start_date: Mapped[date | None] = mapped_column(Date)
    target_date: Mapped[date | None] = mapped_column(Date)
    review_cadence: Mapped[str | None] = mapped_column(Text)
    last_reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    parent_goal_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("work.goals.id", ondelete="SET NULL")
    )
    project_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("work.projects.id", ondelete="SET NULL")
    )
    memory_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("memory.memory_items.id", ondelete="SET NULL")
    )


class Task(UUIDPkMixin, TenantScopedMixin, UserScopedMixin, TimestampsMixin, Base):
    __tablename__ = "tasks"
    __table_args__ = (
        CheckConstraint(
            "status IN ('todo','in_progress','blocked','done','cancelled')", name="status"
        ),
        CheckConstraint("priority >= 1 AND priority <= 5", name="priority_range"),
        CheckConstraint("source IN ('user','agent','commitment','event')", name="source"),
        Index("ix_tasks_user_status_due", "user_id", "status", "due_at"),
        Index(
            "ix_tasks_user_remind_at",
            "user_id",
            "remind_at",
            postgresql_where=text("status NOT IN ('done','cancelled')"),
        ),
        {"schema": "work"},
    )

    title: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'todo'"))
    priority: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default=text("3"))
    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    remind_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    source: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'user'"))
    source_ref: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    goal_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("work.goals.id", ondelete="SET NULL")
    )
    project_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("work.projects.id", ondelete="SET NULL")
    )
    commitment_memory_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("memory.memory_items.id", ondelete="SET NULL")
    )
    contact_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("crm.contacts.id", ondelete="SET NULL")
    )
    recurrence: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    created_by_run_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("agent.agent_runs.id", ondelete="SET NULL")
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))

    __mapper_args__ = {"version_id_col": version}  # noqa: RUF012
