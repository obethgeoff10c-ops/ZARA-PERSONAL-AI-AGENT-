"""Append-only audit log (02-DATABASE.md §2.11)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, Index, LargeBinary, Text, text
from sqlalchemy.dialects.postgresql import INET, JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from zara.db.base import Base, TenantScopedMixin, UUIDPkMixin


class AuditLog(UUIDPkMixin, TenantScopedMixin, Base):
    __tablename__ = "audit_log"
    __table_args__ = (
        Index("ix_audit_log_tenant_created", "tenant_id", text("created_at DESC")),
        Index("ix_audit_log_user_created", "user_id", text("created_at DESC")),
        Index("ix_audit_log_resource", "resource_type", "resource_id"),
        {"schema": "audit"},
    )

    user_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("core.users.id", ondelete="SET NULL")
    )
    actor_type: Mapped[str] = mapped_column(Text, nullable=False)
    actor_id: Mapped[str | None] = mapped_column(Text)
    action: Mapped[str] = mapped_column(Text, nullable=False)
    resource_type: Mapped[str | None] = mapped_column(Text)
    resource_id: Mapped[str | None] = mapped_column(Text)
    run_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True))
    before_hash: Mapped[bytes | None] = mapped_column(LargeBinary)
    after_hash: Mapped[bytes | None] = mapped_column(LargeBinary)
    metadata_json: Mapped[dict[str, Any] | None] = mapped_column("metadata", JSONB)
    ip: Mapped[str | None] = mapped_column(INET)
    trace_id: Mapped[str | None] = mapped_column(Text)
    prev_hash: Mapped[bytes | None] = mapped_column(LargeBinary)
    row_hash: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
