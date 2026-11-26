"""negative memory (blocked facts)

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-25 00:00:00.000000

Adds ``memory.blocked_facts`` so a deleted/forgotten fact cannot be re-created
by a later sync of the same source (03-MEMORY-ENGINE §7).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "blocked_facts",
        sa.Column("fact_hash", sa.LargeBinary(), nullable=False),
        sa.Column("canonical_key", sa.Text(), nullable=True),
        sa.Column("reason", sa.Text(), server_default=sa.text("'deleted'"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["core.tenants.id"],
            name=op.f("fk_blocked_facts_tenant_id_tenants"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["core.users.id"],
            name=op.f("fk_blocked_facts_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_blocked_facts")),
        sa.UniqueConstraint("user_id", "fact_hash", name="uq_blocked_facts_user_hash"),
        schema="memory",
    )
    op.create_index(
        op.f("ix_memory_blocked_facts_tenant_id"),
        "blocked_facts",
        ["tenant_id"],
        unique=False,
        schema="memory",
    )
    op.create_index(
        op.f("ix_memory_blocked_facts_user_id"),
        "blocked_facts",
        ["user_id"],
        unique=False,
        schema="memory",
    )
    op.create_index(
        "ix_blocked_facts_user_key",
        "blocked_facts",
        ["user_id", "canonical_key"],
        unique=False,
        schema="memory",
    )

    op.execute("ALTER TABLE memory.blocked_facts ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE memory.blocked_facts FORCE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY tenant_isolation ON memory.blocked_facts
        USING (tenant_id = current_setting('app.tenant_id', true)::uuid)
        WITH CHECK (tenant_id = current_setting('app.tenant_id', true)::uuid)
        """
    )
    op.execute(
        """
        CREATE POLICY user_isolation ON memory.blocked_facts
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
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON memory.blocked_facts TO zara_app")
    op.execute("GRANT SELECT ON memory.blocked_facts TO zara_readonly")


def downgrade() -> None:
    op.drop_table("blocked_facts", schema="memory")
