"""Unit tests for cursor pagination helpers."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import Select
from zara.api.deps import parse_uuid
from zara.api.pagination import (
    MAX_PAGE_SIZE,
    decode_cursor,
    encode_cursor,
    paginate,
)
from zara.errors import AppError
from zara.models import Task
from zara.schemas import Page


def _row(created_at: datetime) -> SimpleNamespace:
    return SimpleNamespace(created_at=created_at, id=uuid.uuid4())


class _Scalars:
    def __init__(self, rows: list[SimpleNamespace]) -> None:
        self._rows = rows

    def __iter__(self):
        return iter(self._rows)


class _Result:
    def __init__(self, rows: list[SimpleNamespace]) -> None:
        self._rows = rows

    def scalars(self) -> _Scalars:
        return _Scalars(self._rows)


class _FakeSession:
    """Records the LIMIT applied by ``paginate`` without touching a database."""

    def __init__(self, rows: list[SimpleNamespace]) -> None:
        self._rows = rows
        self.applied_limits: list[object] = []

    async def execute(self, statement: Select[object]) -> _Result:
        self.applied_limits.append(statement._limit)
        return _Result(self._rows)


def test_encode_decode_round_trip() -> None:
    created_at = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
    row_id = uuid.uuid4()

    cursor = encode_cursor(created_at, row_id)
    decoded_at, decoded_id = decode_cursor(cursor)

    assert "+" not in cursor and "/" not in cursor
    assert decoded_id == row_id
    assert decoded_at == created_at


@pytest.mark.parametrize("cursor", ["", "not-base64!!!", "e30", "eyJ0IjoieCJ9"])
def test_invalid_cursor_raises_validation_failed(cursor: str) -> None:
    with pytest.raises(AppError) as excinfo:
        decode_cursor(cursor)

    assert excinfo.value.status_code == 422
    assert excinfo.value.code == "validation_error"
    assert excinfo.value.errors == [{"field": "cursor", "message": "Invalid cursor"}]


async def test_paginate_clamps_page_size_and_returns_next_cursor() -> None:
    now = datetime.now(UTC)
    rows = [_row(now - timedelta(seconds=index)) for index in range(MAX_PAGE_SIZE + 5)]
    session = _FakeSession(rows)

    page, next_cursor = await paginate(session, Select(Task), limit=10_000, cursor=None, model=Task)

    assert session.applied_limits == [MAX_PAGE_SIZE + 1]
    assert len(page) == MAX_PAGE_SIZE
    assert next_cursor is not None
    # The returned cursor must resolve back to the last row of the first page.
    decoded_at, decoded_id = decode_cursor(next_cursor)
    assert decoded_at == page[-1].created_at
    assert decoded_id == page[-1].id


async def test_paginate_small_last_page_has_no_cursor() -> None:
    now = datetime.now(UTC)
    rows = [_row(now - timedelta(seconds=index)) for index in range(3)]
    session = _FakeSession(rows)

    page, next_cursor = await paginate(session, Select(Task), limit=50, cursor=None, model=Task)

    assert len(page) == 3
    assert next_cursor is None


def test_page_envelope_fields_and_default_cursor() -> None:
    envelope: Page[int] = Page(items=[1, 2])
    assert envelope.items == [1, 2]
    assert envelope.next_cursor is None


def test_parse_uuid_validation_error() -> None:
    valid = uuid.uuid4()
    assert parse_uuid(str(valid)) == valid

    with pytest.raises(AppError) as excinfo:
        parse_uuid("not-a-uuid", field="conversation_id")

    assert excinfo.value.status_code == 422
    assert excinfo.value.code == "validation_error"
    assert excinfo.value.errors == [{"field": "conversation_id", "message": "Invalid UUID"}]
