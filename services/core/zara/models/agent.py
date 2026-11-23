"""Conversation, run ledger, run events, tool executions, and approvals."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    SmallInteger,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from zara.db.base import Base, TenantScopedMixin, TimestampsMixin, UserScopedMixin, UUIDPkMixin


class Conversation(UUIDPkMixin, TenantScopedMixin, UserScopedMixin, TimestampsMixin, Base):
    __tablename__ = "conversations"
    __table_args__ = (
        CheckConstraint("title_source IN ('auto','user')", name="title_source"),
        Index(
            "ix_conversations_user_last_message",
            "user_id",
            text("last_message_at DESC"),
            postgresql_where=text("archived_at IS NULL"),
        ),
        {"schema": "agent"},
    )

    title: Mapped[str | None] = mapped_column(Text)
    title_source: Mapped[str | None] = mapped_column(Text, server_default=text("'auto'"))
    last_message_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    metadata_json: Mapped[dict[str, Any] | None] = mapped_column("metadata", JSONB)


class Message(UUIDPkMixin, TenantScopedMixin, UserScopedMixin, Base):
    __tablename__ = "messages"
    __table_args__ = (
        CheckConstraint("role IN ('user','assistant','system_notice')", name="role"),
        CheckConstraint("feedback IS NULL OR feedback IN ('up','down')", name="feedback"),
        Index("ix_messages_conversation_created", "conversation_id", "created_at"),
        {"schema": "agent"},
    )

    conversation_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("agent.conversations.id", ondelete="CASCADE"),
        nullable=False,
    )
    role: Mapped[str] = mapped_column(Text, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    content_blocks: Mapped[list[Any] | None] = mapped_column(JSONB)
    run_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("agent.agent_runs.id", ondelete="SET NULL")
    )
    parent_message_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("agent.messages.id", ondelete="SET NULL")
    )
    token_count: Mapped[int | None] = mapped_column(Integer)
    citations: Mapped[list[Any] | None] = mapped_column(JSONB)
    feedback: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


class AgentRun(UUIDPkMixin, TenantScopedMixin, UserScopedMixin, TimestampsMixin, Base):
    __tablename__ = "agent_runs"
    __table_args__ = (
        CheckConstraint(
            "trigger_type IN ('user_message','event','schedule','proactive','manual','system')",
            name="trigger_type",
        ),
        CheckConstraint(
            "status IN ('queued','running','awaiting_approval','paused','succeeded',"
            "'failed','cancelled','expired')",
            name="status",
        ),
        CheckConstraint("taint_level IN ('clean','tainted')", name="taint_level"),
        Index("ix_agent_runs_user_created", "user_id", text("created_at DESC")),
        Index(
            "ix_agent_runs_active_status",
            "status",
            postgresql_where=text("status IN ('queued','running','awaiting_approval','paused')"),
        ),
        Index("ix_agent_runs_workflow_id", "workflow_id"),
        Index("ix_agent_runs_trace_id", "trace_id"),
        {"schema": "agent"},
    )

    conversation_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("agent.conversations.id", ondelete="SET NULL")
    )
    trigger_type: Mapped[str] = mapped_column(Text, nullable=False)
    trigger_ref: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    goal: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    domains: Mapped[list[str] | None] = mapped_column(ARRAY(Text))
    routing: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    taint_level: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'clean'"))
    autonomy_level: Mapped[str | None] = mapped_column(Text)
    budget: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    usage: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    state_snapshot: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    workflow_id: Mapped[str | None] = mapped_column(Text)
    workflow_run_id: Mapped[str | None] = mapped_column(Text)
    trace_id: Mapped[str | None] = mapped_column(Text)
    error: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    final_output: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class RunEvent(TenantScopedMixin, Base):
    __tablename__ = "run_events"
    __table_args__ = (
        UniqueConstraint("run_id", "seq", name="uq_run_events_run_id_seq"),
        Index("ix_run_events_run_id_seq", "run_id", "seq"),
        {"schema": "agent"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    run_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("agent.agent_runs.id", ondelete="CASCADE"),
        nullable=False,
    )
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    type: Mapped[str] = mapped_column(Text, nullable=False)
    data: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


class ToolExecution(UUIDPkMixin, TenantScopedMixin, UserScopedMixin, TimestampsMixin, Base):
    __tablename__ = "tool_executions"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "tool_name",
            "idempotency_key",
            name="uq_tool_executions_tenant_tool_idem",
        ),
        CheckConstraint("risk_tier >= 0 AND risk_tier <= 4", name="risk_tier"),
        CheckConstraint(
            "policy_decision IN ('allow','require_approval','deny')", name="policy_decision"
        ),
        CheckConstraint(
            "status IN ('pending','running','succeeded','failed','timed_out','denied',"
            "'cancelled','compensated')",
            name="status",
        ),
        Index("ix_tool_executions_run_id", "run_id"),
        Index("ix_tool_executions_user_created", "user_id", text("created_at DESC")),
        Index("ix_tool_executions_status", "status"),
        {"schema": "agent"},
    )

    run_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("agent.agent_runs.id", ondelete="SET NULL")
    )
    step_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True))
    tool_name: Mapped[str] = mapped_column(Text, nullable=False)
    tool_version: Mapped[str] = mapped_column(Text, nullable=False)
    risk_tier: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    args: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    args_hash: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(Text, nullable=False)
    policy_decision: Mapped[str] = mapped_column(Text, nullable=False)
    policy_reasons: Mapped[list[Any] | None] = mapped_column(JSONB)
    approval_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey(
            "agent.approvals.id",
            use_alter=True,
            name="fk_tool_executions_approval_id_approvals",
            ondelete="SET NULL",
        ),
    )
    status: Mapped[str] = mapped_column(Text, nullable=False)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    result_summary: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    result_ref: Mapped[str | None] = mapped_column(Text)
    error: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    taint_in: Mapped[bool | None] = mapped_column(Boolean)
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    external_ref: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Approval(UUIDPkMixin, TenantScopedMixin, UserScopedMixin, TimestampsMixin, Base):
    __tablename__ = "approvals"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending','approved','rejected','expired','cancelled','edited_approved')",
            name="status",
        ),
        CheckConstraint(
            "status <> 'approved' OR decided_at IS NOT NULL", name="approved_requires_decision"
        ),
        Index("ix_approvals_user_status_expires", "user_id", "status", "expires_at"),
        {"schema": "agent"},
    )

    run_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("agent.agent_runs.id", ondelete="SET NULL")
    )
    tool_execution_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("agent.tool_executions.id", ondelete="SET NULL")
    )
    tool_name: Mapped[str | None] = mapped_column(Text)
    risk_tier: Mapped[int | None] = mapped_column(SmallInteger)
    args_hash: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    preview: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    reasons: Mapped[list[Any] | None] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'pending'"))
    requested_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    decided_via: Mapped[str | None] = mapped_column(Text)
    decision_note: Mapped[str | None] = mapped_column(Text)
    edited_args_hash: Mapped[bytes | None] = mapped_column(LargeBinary)
    step_up_verified: Mapped[bool | None] = mapped_column(Boolean, server_default=text("false"))
    workflow_id: Mapped[str | None] = mapped_column(Text)
