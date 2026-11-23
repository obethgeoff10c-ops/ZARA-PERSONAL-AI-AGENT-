"""Agent run, event, and tool-execution contracts."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class RunCreate(BaseModel):
    goal: str = Field(min_length=1, max_length=2000)
    domains: list[str] | None = Field(default=None, max_length=8)
    conversation_id: uuid.UUID | None = None
    mode: str | None = Field(default=None, pattern="^(background|interactive)$")
    budget: dict[str, Any] | None = None


class RunCancelIn(BaseModel):
    reason: str | None = Field(default=None, max_length=500)


class RunOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    conversation_id: uuid.UUID | None
    goal: str
    status: str
    trigger_type: str
    domains: list[str] | None
    routing: dict[str, Any] | None
    taint_level: str
    autonomy_level: str | None
    budget: dict[str, Any] | None
    usage: dict[str, Any] | None
    error: dict[str, Any] | None
    final_output: dict[str, Any] | None
    workflow_id: str | None
    trace_id: str | None
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


class RunEventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    seq: int
    type: str
    data: dict[str, Any]
    created_at: datetime


class ToolExecutionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    tool_name: str
    tool_version: str
    risk_tier: int
    status: str
    policy_decision: str
    args: dict[str, Any]
    policy_reasons: list[Any] | None
    result_summary: dict[str, Any] | None
    error: dict[str, Any] | None
    approval_id: uuid.UUID | None
    latency_ms: int | None
    started_at: datetime | None
    finished_at: datetime | None
    created_at: datetime


class RunCancelOut(BaseModel):
    status: str
