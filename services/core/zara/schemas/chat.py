"""Chat request contract (07-API.md §2)."""

from __future__ import annotations

import uuid
from typing import Literal

from pydantic import BaseModel, Field


class ChatAttachment(BaseModel):
    id: uuid.UUID | None = None
    name: str | None = Field(default=None, max_length=300)
    mime_type: str | None = Field(default=None, max_length=120)


class ChatIn(BaseModel):
    conversation_id: uuid.UUID | None = None
    message: str = Field(min_length=1, max_length=20_000)
    attachments: list[ChatAttachment] = Field(default_factory=list, max_length=20)
    client_message_id: uuid.UUID | None = None
    mode: Literal["auto", "quick", "deep"] = "auto"
