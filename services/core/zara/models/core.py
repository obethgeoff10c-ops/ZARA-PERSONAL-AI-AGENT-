"""Core identity, tenancy, and preference models (02-DATABASE.md §2.1)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    LargeBinary,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import CITEXT, INET, JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from zara.db.base import (
    Base,
    TenantScopedMixin,
    TimestampsMixin,
    UserScopedMixin,
    UUIDPkMixin,
)


class Tenant(UUIDPkMixin, TimestampsMixin, Base):
    __tablename__ = "tenants"
    __table_args__ = (
        CheckConstraint("plan IN ('free','pro','team','enterprise')", name="plan"),
        CheckConstraint("status IN ('active','suspended','deleting')", name="status"),
        {"schema": "core"},
    )

    name: Mapped[str] = mapped_column(Text, nullable=False)
    slug: Mapped[str] = mapped_column(CITEXT, nullable=False, unique=True)
    plan: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'free'"))
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'active'"))


class User(UUIDPkMixin, TenantScopedMixin, TimestampsMixin, Base):
    __tablename__ = "users"
    __table_args__ = (
        UniqueConstraint(
            "auth_provider",
            "external_auth_id",
            name="uq_users_auth_provider_external_auth_id",
        ),
        UniqueConstraint("tenant_id", "email", name="uq_users_tenant_id_email"),
        CheckConstraint("role IN ('owner','admin','member')", name="role"),
        CheckConstraint("status IN ('active','disabled','deleting')", name="status"),
        {"schema": "core"},
    )

    external_auth_id: Mapped[str] = mapped_column(Text, nullable=False)
    auth_provider: Mapped[str] = mapped_column(Text, nullable=False)
    email: Mapped[str] = mapped_column(CITEXT, nullable=False)
    email_verified: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    display_name: Mapped[str | None] = mapped_column(Text)
    role: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'owner'"))
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'active'"))
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AuthSession(UUIDPkMixin, TenantScopedMixin, Base):
    __tablename__ = "auth_sessions"
    __table_args__ = (
        Index("ix_auth_sessions_user_id_revoked_at", "user_id", "revoked_at"),
        Index("ix_auth_sessions_expires_at", "expires_at"),
        {"schema": "core"},
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("core.users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    session_token_hash: Mapped[bytes] = mapped_column(LargeBinary, nullable=False, unique=True)
    idp_session_id: Mapped[str | None] = mapped_column(Text)
    ip: Mapped[str | None] = mapped_column(INET)
    user_agent: Mapped[str | None] = mapped_column(Text)
    device_label: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoke_reason: Mapped[str | None] = mapped_column(Text)


class UserProfile(TenantScopedMixin, TimestampsMixin, Base):
    __tablename__ = "user_profiles"
    __table_args__ = {"schema": "core"}  # noqa: RUF012

    user_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("core.users.id", ondelete="CASCADE"),
        primary_key=True,
    )
    timezone: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'UTC'"))
    locale: Mapped[str | None] = mapped_column(Text, server_default=text("'en'"))
    working_hours: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    quiet_hours: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    communication_style: Mapped[str | None] = mapped_column(Text)
    job_title: Mapped[str | None] = mapped_column(Text)
    company: Mapped[str | None] = mapped_column(Text)
    bio_summary: Mapped[str | None] = mapped_column(Text)
    onboarding_state: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )


class UserPreference(UUIDPkMixin, TenantScopedMixin, UserScopedMixin, TimestampsMixin, Base):
    __tablename__ = "user_preferences"
    __table_args__ = (
        UniqueConstraint("user_id", "namespace", "key", name="uq_user_preferences_user_ns_key"),
        CheckConstraint("source IN ('user','inferred_confirmed','default')", name="source"),
        {"schema": "core"},
    )

    namespace: Mapped[str] = mapped_column(Text, nullable=False)
    key: Mapped[str] = mapped_column(Text, nullable=False)
    value: Mapped[Any] = mapped_column(JSONB, nullable=False)
    source: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'user'"))


class FeatureFlag(UUIDPkMixin, TimestampsMixin, Base):
    __tablename__ = "feature_flags"
    __table_args__ = (
        Index(
            "uq_feature_flags_scope_key",
            text("coalesce(tenant_id, '00000000-0000-0000-0000-000000000000'::uuid)"),
            text("coalesce(user_id, '00000000-0000-0000-0000-000000000000'::uuid)"),
            "key",
            unique=True,
        ),
        {"schema": "core"},
    )

    tenant_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("core.tenants.id", ondelete="CASCADE"),
        index=True,
    )
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("core.users.id", ondelete="CASCADE"),
        index=True,
    )
    key: Mapped[str] = mapped_column(Text, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False)
    config: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
