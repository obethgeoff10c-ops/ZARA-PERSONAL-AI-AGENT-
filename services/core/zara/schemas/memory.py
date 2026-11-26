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


# --- extraction -------------------------------------------------------------


class MemoryExtractIn(BaseModel):
    """Raw text span to run through the extraction pipeline."""

    text: str = Field(min_length=1, max_length=50_000)
    source_id: str = Field(min_length=1, max_length=200)
    source_type: str = Field(default="message", pattern=r"^(message|document|event|agent_run)$")
    trust_level: str = Field(default="user", pattern=r"^(user|zara|external_untrusted)$")
    message_id: uuid.UUID | None = None
    run_id: uuid.UUID | None = None
    reference_time: datetime | None = None


class MemoryExtractionRejection(BaseModel):
    reason: str
    candidate: dict[str, Any]


class MemoryExtractOut(BaseModel):
    accepted: list[MemoryOut] = Field(default_factory=list)
    suggested: list[MemoryOut] = Field(default_factory=list)
    rejected: list[MemoryExtractionRejection] = Field(default_factory=list)


# --- retrieval --------------------------------------------------------------


class MemoryRetrieveIn(BaseModel):
    query: str = Field(min_length=1, max_length=2000)
    kinds: list[str] | None = Field(default=None, max_length=10)
    domains: list[str] = Field(default_factory=list, max_length=10)
    entity_ids: list[uuid.UUID] = Field(default_factory=list, max_length=20)
    k: int = Field(default=12, ge=1, le=50)
    token_budget: int = Field(default=1800, ge=100, le=20_000)
    include_history: bool = False


class MemoryRetrievalSource(BaseModel):
    source_type: str
    quote: str | None = None
    observed_at: str | None = None


class MemoryRetrievedOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    kind: str
    title: str | None
    content: str
    score: float
    components: dict[str, float]
    sources: list[MemoryRetrievalSource] = Field(default_factory=list)
    importance: float
    confidence: float
    sensitivity: str
    status: str
    occurred_at: str | None = None
    due_at: datetime | None = None


class MemoryRetrieveOut(BaseModel):
    items: list[MemoryRetrievedOut] = Field(default_factory=list)
    retrieval_log_id: str | None = None


# --- personalization --------------------------------------------------------


class PersonalizationMemoryOut(BaseModel):
    id: uuid.UUID
    kind: str
    title: str | None
    content: str
    score: float
    importance: float
    confidence: float
    sources: list[MemoryRetrievalSource] = Field(default_factory=list)
    occurred_at: str | None = None
    due_at: datetime | None = None


class PersonalizeOut(BaseModel):
    user_id: uuid.UUID
    tenant_id: uuid.UUID
    generated_at: datetime
    communication_style: str | None = None
    timezone: str = "UTC"
    locale: str = "en"
    preferences: list[PersonalizationMemoryOut] = Field(default_factory=list)
    goals: list[PersonalizationMemoryOut] = Field(default_factory=list)
    commitments: list[PersonalizationMemoryOut] = Field(default_factory=list)
    projects: list[PersonalizationMemoryOut] = Field(default_factory=list)
    relevant_people: list[PersonalizationMemoryOut] = Field(default_factory=list)
    recent_memories: list[PersonalizationMemoryOut] = Field(default_factory=list)


class ConsolidationOut(BaseModel):
    decayed: int = 0
    archived: int = 0
    expired: int = 0
