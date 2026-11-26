"""OAuth state store with encrypted PKCE verifier

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-25 00:10:00.000000

Adds ``integrations.oauth_states`` (09-SECURITY-TESTING §1): 256-bit state
hashed at rest, single use, 10-minute TTL, PKCE verifier encrypted with the
same envelope-encryption scheme as provider credentials.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "oauth_states",
        sa.Column("state_hash", sa.LargeBinary(), nullable=False),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column(
            "requested_scopes",
            postgresql.ARRAY(sa.Text()),
            server_default=sa.text("'{}'::text[]"),
            nullable=False,
        ),
        sa.Column("code_verifier_ct", sa.LargeBinary(), nullable=False),
        sa.Column("dek_wrapped", sa.LargeBinary(), nullable=False),
        sa.Column("kms_key_id", sa.Text(), nullable=False),
        sa.Column("enc_version", sa.SmallInteger(), server_default=sa.text("1"), nullable=False),
        sa.Column("redirect_after", sa.Text(), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.CheckConstraint(
            "provider IN ('gmail','gcal','gdrive','github')",
            name=op.f("ck_oauth_states_provider"),
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["core.tenants.id"],
            name=op.f("fk_oauth_states_tenant_id_tenants"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["core.users.id"],
            name=op.f("fk_oauth_states_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_oauth_states")),
        sa.UniqueConstraint("state_hash", name=op.f("uq_oauth_states_state_hash")),
        schema="integrations",
    )
    op.create_index(
        op.f("ix_integrations_oauth_states_tenant_id"),
        "oauth_states",
        ["tenant_id"],
        unique=False,
        schema="integrations",
    )
    op.create_index(
        op.f("ix_integrations_oauth_states_user_id"),
        "oauth_states",
        ["user_id"],
        unique=False,
        schema="integrations",
    )
    op.create_index(
        "ix_oauth_states_user_created",
        "oauth_states",
        ["user_id", "created_at"],
        unique=False,
        schema="integrations",
    )

    op.execute("ALTER TABLE integrations.oauth_states ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE integrations.oauth_states FORCE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY tenant_isolation ON integrations.oauth_states
        USING (tenant_id = current_setting('app.tenant_id', true)::uuid)
        WITH CHECK (tenant_id = current_setting('app.tenant_id', true)::uuid)
        """
    )
    op.execute(
        """
        CREATE POLICY user_isolation ON integrations.oauth_states
        USING (
            current_setting('app.role', true) = 'service'
            OR user_id = current_setting('app.user_id', true)::uuid
        )
        WITH CHECK (
            current_setting('app.role', true) = 'service'
            OR user_id = current_setting('app.user_id', true)::uuid
        )
        """
    )
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON integrations.oauth_states TO zara_app")
    op.execute("GRANT SELECT ON integrations.oauth_states TO zara_readonly")


def downgrade() -> None:
    op.drop_table("oauth_states", schema="integrations")
