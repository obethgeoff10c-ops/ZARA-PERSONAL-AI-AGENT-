from __future__ import annotations

import uuid

import pytest
from zara.db.base import new_uuid7
from zara.db.session import rls_session


def test_uuid7_has_version_7() -> None:
    value = new_uuid7()

    assert isinstance(value, uuid.UUID)
    assert value.version == 7


def test_uuid7_is_unique_and_ordered_by_generation() -> None:
    values = [new_uuid7() for _ in range(1_000)]

    assert len(set(values)) == 1_000
    assert values == sorted(values)


async def test_rls_session_rejects_unknown_role() -> None:
    with pytest.raises(ValueError, match="Unsupported database role context"):
        async with rls_session(
            tenant_id=uuid.uuid4(),
            user_id=uuid.uuid4(),
            role="admin",
        ):
            pass  # pragma: no cover - the context manager must reject first
