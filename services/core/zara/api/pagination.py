"""Opaque cursor pagination helpers.

Cursors encode the last row's ``(created_at, id)`` pair. They are opaque to
clients and validated server-side, so a forged cursor yields a 422 instead of a
database error.
"""

from __future__ import annotations

import base64
import binascii
import json
import uuid
from datetime import datetime
from typing import Any, Literal, TypeVar, cast

from sqlalchemy import Select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from zara.errors import validation_failed

T = TypeVar("T")

MAX_PAGE_SIZE = 100


def encode_cursor(created_at: datetime, row_id: uuid.UUID) -> str:
    payload = json.dumps(
        {"t": created_at.isoformat(), "id": str(row_id)}, separators=(",", ":")
    ).encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def decode_cursor(cursor: str) -> tuple[datetime, uuid.UUID]:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        raw = base64.urlsafe_b64decode(padded.encode("ascii"))
        payload: Any = json.loads(raw)
        created_at = datetime.fromisoformat(str(payload["t"]))
        row_id = uuid.UUID(str(payload["id"]))
    except (ValueError, KeyError, TypeError, binascii.Error, json.JSONDecodeError):
        raise validation_failed(
            "The pagination cursor is invalid.",
            errors=[{"field": "cursor", "message": "Invalid cursor"}],
        ) from None
    return created_at, row_id


async def paginate(
    db: AsyncSession,
    stmt: Select[Any],
    *,
    limit: int,
    cursor: str | None,
    descending: bool = True,
    model: Any = None,
) -> tuple[list[Any], str | None]:
    """Apply keyset pagination and return ``(rows, next_cursor)``.

    ``model`` must expose ``created_at`` and ``id``.
    """
    bounded = max(1, min(limit, MAX_PAGE_SIZE))
    query = stmt
    if cursor:
        created_at, row_id = decode_cursor(cursor)
        operator: Literal["<", ">"] = "<" if descending else ">"
        if operator == "<":
            query = query.where(tuple_(model.created_at, model.id) < tuple_(created_at, row_id))
        else:
            query = query.where(tuple_(model.created_at, model.id) > tuple_(created_at, row_id))

    if descending:
        query = query.order_by(model.created_at.desc(), model.id.desc())
    else:
        query = query.order_by(model.created_at.asc(), model.id.asc())

    result = await db.execute(query.limit(bounded + 1))
    rows = list(result.scalars())
    has_more = len(rows) > bounded
    rows = rows[:bounded]
    next_cursor = (
        encode_cursor(cast(datetime, rows[-1].created_at), rows[-1].id)
        if has_more and rows
        else None
    )
    return rows, next_cursor
