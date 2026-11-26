"""Integration contracts (metadata only; credentials never leave the DB)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict


class IntegrationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    provider: str
    account_identifier: str
    status: str
    granted_scopes: list[str]
    settings: dict[str, Any]
    last_sync_at: datetime | None
    last_error: str | None
    error_count: int
    connected_at: datetime | None
    revoked_at: datetime | None
    created_at: datetime


class IntegrationSettingsUpdate(BaseModel):
    settings: dict[str, Any]


class IntegrationConnectIn(BaseModel):
    scopes: list[str] | None = None
    redirect_after: str | None = None


class IntegrationConnectOut(BaseModel):
    authorization_url: str
    state: str
    expires_at: datetime


class IntegrationSyncIn(BaseModel):
    mode: Literal["incremental", "reconcile"] = "incremental"


class IntegrationActionOut(BaseModel):
    id: uuid.UUID
    status: str


class SyncAcceptedOut(BaseModel):
    workflow_id: str
    status: Literal["queued"] = "queued"
