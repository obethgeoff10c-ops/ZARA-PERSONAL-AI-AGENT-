"""Session management and per-transaction tenant context.

Every request/worker transaction that touches tenant data must run inside
``rls_session``. It sets the Postgres session variables that the RLS policies
read, so a query that forgets a ``WHERE tenant_id = ...`` clause cannot leak
another tenant's rows (02-DATABASE.md §0).
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from zara.db.engine import get_sessionmaker

_SET_CONTEXT = text(
    "SELECT set_config('app.tenant_id', :tenant_id, true),"
    " set_config('app.user_id', :user_id, true),"
    " set_config('app.role', :role, true)"
)

# Every request runs as the RLS-enforcing application role, even when the
# connection was opened by the database owner (local dev, CI). ``SET LOCAL`` is
# transaction-scoped, so a pooled connection never leaks its role or context.
_ACTIVATE_APP_ROLE = text("SET LOCAL ROLE zara_app")


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """Transactional session: commit on success, roll back on error."""
    session_factory = get_sessionmaker()
    async with session_factory() as session:
        try:
            yield session
            await session.commit()
        except BaseException:
            await session.rollback()
            raise


@asynccontextmanager
async def rls_session(
    *,
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
    role: str = "user",
) -> AsyncIterator[AsyncSession]:
    """Session scoped to one tenant/user for the lifetime of the transaction."""
    if role not in {"user", "service"}:
        raise ValueError(f"Unsupported database role context: {role!r}")

    async with session_scope() as session:
        await session.execute(_ACTIVATE_APP_ROLE)
        await session.execute(
            _SET_CONTEXT,
            {
                "tenant_id": str(tenant_id),
                "user_id": str(user_id),
                "role": role,
            },
        )
        yield session
