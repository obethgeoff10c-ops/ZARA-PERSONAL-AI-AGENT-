"""Approval contracts. Decisions are bound to the exact ``args_hash``."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ApprovalOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    run_id: uuid.UUID | None
    tool_execution_id: uuid.UUID | None
    tool_name: str | None
    risk_tier: int | None
    preview: dict[str, Any]
    reasons: list[Any] | None
    status: str
    args_hash: str
    requested_at: datetime | None
    expires_at: datetime
    decided_at: datetime | None
    decided_via: str | None
    decision_note: str | None
    step_up_verified: bool | None

    @field_validator("args_hash", mode="before")
    @classmethod
    def _hex(cls, value: object) -> object:
        return value.hex() if isinstance(value, (bytes, bytearray)) else value


class ApprovalApproveIn(BaseModel):
    args_hash: str = Field(min_length=64, max_length=64)
    note: str | None = Field(default=None, max_length=2000)


class ApprovalEditIn(BaseModel):
    edits: dict[str, Any]
    note: str | None = Field(default=None, max_length=2000)


class ApprovalRejectIn(BaseModel):
    reason: str | None = Field(default=None, max_length=2000)
