"""Activity feed contracts."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel


class ActivityItem(BaseModel):
    id: uuid.UUID
    at: datetime
    type: str
    action: str
    summary: str
    resource_type: str | None
    resource_id: str | None
    run_id: uuid.UUID | None
    actor: str


class AuditItem(BaseModel):
    id: uuid.UUID
    tenant_id: uuid.UUID
    user_id: uuid.UUID | None
    actor_type: str
    action: str
    resource_type: str | None
    resource_id: str | None
    run_id: uuid.UUID | None
    metadata: dict[str, object] | None
    trace_id: str | None
    created_at: datetime


class ChainCheckOut(BaseModel):
    valid: bool
    broken_ids: list[uuid.UUID]
