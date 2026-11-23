"""Task contracts."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class TaskCreate(BaseModel):
    title: str = Field(min_length=1, max_length=300)
    description: str | None = Field(default=None, max_length=10_000)
    due_at: datetime | None = None
    remind_at: datetime | None = None
    priority: int = Field(default=3, ge=1, le=5)
    project_id: uuid.UUID | None = None
    goal_id: uuid.UUID | None = None
    contact_id: uuid.UUID | None = None
    recurrence: dict[str, Any] | None = None


class TaskUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=300)
    description: str | None = Field(default=None, max_length=10_000)
    status: Literal["todo", "in_progress", "blocked", "done", "cancelled"] | None = None
    priority: int | None = Field(default=None, ge=1, le=5)
    due_at: datetime | None = None
    remind_at: datetime | None = None
    project_id: uuid.UUID | None = None
    goal_id: uuid.UUID | None = None
    contact_id: uuid.UUID | None = None
    recurrence: dict[str, Any] | None = None


class TaskCompleteIn(BaseModel):
    note: str | None = Field(default=None, max_length=2000)


class TaskOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    title: str
    description: str | None
    status: str
    priority: int
    due_at: datetime | None
    remind_at: datetime | None
    completed_at: datetime | None
    source: str
    goal_id: uuid.UUID | None
    project_id: uuid.UUID | None
    contact_id: uuid.UUID | None
    commitment_memory_id: uuid.UUID | None
    recurrence: dict[str, Any] | None
    created_at: datetime
    updated_at: datetime
    version: int
