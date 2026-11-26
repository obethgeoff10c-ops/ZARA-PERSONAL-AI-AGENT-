"""Integration status and credential metadata (02-DATABASE.md §2.2).

Ciphertext columns are never selected by request handlers. Envelope encryption
lands with the integration milestone; until then this table is schema-only.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
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


class Integration(UUIDPkMixin, TenantScopedMixin, UserScopedMixin, TimestampsMixin, Base):
    __tablename__ = "integrations"
    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "provider",
            "account_identifier",
            name="uq_integrations_user_provider_account",
        ),
        CheckConstraint("provider IN ('gmail','gcal','gdrive','github')", name="provider"),
        CheckConstraint(
            "status IN ('pending','active','needs_reauth','paused','error','revoked')",
            name="status",
        ),
        Index("ix_integrations_user_provider_status", "user_id", "provider", "status"),
        {"schema": "integrations"},
    )

    provider: Mapped[str] = mapped_column(Text, nullable=False)
    account_identifier: Mapped[str] = mapped_column(Text, nullable=False)
    external_account_id: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    granted_scopes: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, server_default=text("'{}'::text[]")
    )
    settings: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    last_sync_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    error_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    connected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class OAuthState(UUIDPkMixin, TenantScopedMixin, UserScopedMixin, Base):
    """Single-use, server-side OAuth state with the encrypted PKCE verifier."""

    __tablename__ = "oauth_states"
    __table_args__ = (
        CheckConstraint("provider IN ('gmail','gcal','gdrive','github')", name="provider"),
        Index("ix_oauth_states_user_created", "user_id", "created_at"),
        {"schema": "integrations"},
    )

    state_hash: Mapped[bytes] = mapped_column(LargeBinary, nullable=False, unique=True)
    provider: Mapped[str] = mapped_column(Text, nullable=False)
    requested_scopes: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, server_default=text("'{}'::text[]")
    )
    code_verifier_ct: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    dek_wrapped: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    kms_key_id: Mapped[str] = mapped_column(Text, nullable=False)
    enc_version: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default=text("1"))
    redirect_after: Mapped[str | None] = mapped_column(Text)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


class OAuthCredential(UUIDPkMixin, TenantScopedMixin, TimestampsMixin, Base):
    __tablename__ = "oauth_credentials"
    __table_args__ = ({"schema": "integrations"},)

    integration_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("integrations.integrations.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    token_type: Mapped[str | None] = mapped_column(Text)
    access_token_ct: Mapped[bytes | None] = mapped_column(LargeBinary)
    refresh_token_ct: Mapped[bytes | None] = mapped_column(LargeBinary)
    access_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    dek_wrapped: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    kms_key_id: Mapped[str] = mapped_column(Text, nullable=False)
    enc_version: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default=text("1"))
    rotated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_refreshed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    refresh_lock_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class SyncCursor(UUIDPkMixin, TenantScopedMixin, TimestampsMixin, Base):
    __tablename__ = "sync_cursors"
    __table_args__ = (
        UniqueConstraint("integration_id", "resource", name="uq_sync_cursors_integration_resource"),
        {"schema": "integrations"},
    )

    integration_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("integrations.integrations.id", ondelete="CASCADE"),
        nullable=False,
    )
    resource: Mapped[str] = mapped_column(Text, nullable=False)
    cursor_value: Mapped[str | None] = mapped_column(Text)
    watermark_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    state: Mapped[dict[str, Any] | None] = mapped_column(JSONB)


class WebhookSubscription(UUIDPkMixin, TenantScopedMixin, TimestampsMixin, Base):
    __tablename__ = "webhook_subscriptions"
    __table_args__ = (
        UniqueConstraint("kind", "external_id", name="uq_webhook_subscriptions_kind_external"),
        Index(
            "ix_webhook_subscriptions_active_expiry",
            "expires_at",
            postgresql_where=text("status = 'active'"),
        ),
        {"schema": "integrations"},
    )

    integration_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("integrations.integrations.id", ondelete="CASCADE"),
        nullable=False,
    )
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    external_id: Mapped[str | None] = mapped_column(Text)
    secret_hash: Mapped[bytes | None] = mapped_column(LargeBinary)
    resource_uri: Mapped[str | None] = mapped_column(Text)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'active'"))
