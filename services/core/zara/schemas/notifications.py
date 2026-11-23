"""Notification contracts."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict

from zara.schemas.common import Page


class NotificationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    kind: str | None
    channel: str
    title: str | None
    body: str | None
    action_url: str | None
    priority: str | None
    status: str | None
    created_at: datetime
    sent_at: datetime | None
    read_at: datetime | None


class NotificationPage(Page[NotificationOut]):
    unread_count: int = 0
