"""Memory contracts."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

MemoryKind = str


class MemoryCreate(BaseModel):
    kind: str = Field(min_length=1, max_length=32)
    title: str | None = Field(default=None, max_length=300)
    content: str = Field(min_length=1, max_length=50_000)
    payload: dict[str, Any] = Field(default_factory=dict)
    importance: float = Field(default=0.5, ge=0, le=1)
    confidence: float = Field(default=1.0, ge=0, le=1)
    expires_at: datetime | None = None
    due_at: datetime | None = None
    pin: bool = False


class MemoryUpdate(BaseModel):
    title: str | None = Field(default=None, max_length=300)
    content: str | None = Field(default=None, min_length=1, max_length=50_000)
    payload: dict[str, Any] | None = None
    importance: float | None = Field(default=None, ge=0, le=1)
    pinned: bool | None = None
    expires_at: datetime | None = None


class MemoryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    kind: str
    title: str | None
    content: str
    payload: dict[str, Any]
    importance: float
    confidence: float
    sensitivity: str
    trust_level: str
    status: str
    user_pinned: bool
    user_edited: bool
    valid_from: datetime | None
    valid_to: datetime | None
    due_at: datetime | None
    expires_at: datetime | None
    sources_count: int = 0
    created_at: datetime
    updated_at: datetime
    version: int


class MemorySourceOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    source_type: str
    quote: str | None
    observed_at: datetime


class MemoryDetail(MemoryOut):
    sources: list[MemorySourceOut] = Field(default_factory=list)


class MemorySearchIn(BaseModel):
    query: str = Field(min_length=1, max_length=2000)
    kinds: list[str] | None = Field(default=None, max_length=10)
    limit: int = Field(default=20, ge=1, le=50)
