"""Identity, profile, session, and permission contracts."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field


class AuthConfigOut(BaseModel):
    provider: Literal["local", "clerk", "oidc"]
    issuer: str
    client_id: str | None
    jwks_uri: str | None
    local_mode: bool


class DevTokenIn(BaseModel):
    email: EmailStr
    display_name: str | None = Field(default=None, max_length=200)
    org_id: str | None = Field(default=None, max_length=200)


class DevTokenOut(BaseModel):
    access_token: str
    token_type: Literal["Bearer"] = "Bearer"  # noqa: S105 - token scheme name
    expires_in: int


class SessionCreateIn(BaseModel):
    device_label: str | None = Field(default=None, max_length=120)


class ProfileOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    timezone: str
    locale: str | None
    working_hours: dict[str, Any]
    quiet_hours: dict[str, Any]
    communication_style: str | None
    job_title: str | None
    company: str | None
    bio_summary: str | None
    onboarding_state: dict[str, Any]


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    tenant_id: uuid.UUID
    email: str
    display_name: str | None
    role: str
    status: str
    created_at: datetime
    profile: ProfileOut | None = None


class SessionOut(BaseModel):
    session_id: uuid.UUID
    token: str
    expires_at: datetime
    user: UserOut


class SessionItem(BaseModel):
    id: uuid.UUID
    device_label: str | None
    ip: str | None
    user_agent: str | None
    created_at: datetime
    last_used_at: datetime | None
    expires_at: datetime
    current: bool


class ProfileUpdate(BaseModel):
    display_name: str | None = Field(default=None, max_length=200)
    timezone: str | None = Field(default=None, max_length=64)
    locale: str | None = Field(default=None, max_length=16)
    working_hours: dict[str, Any] | None = None
    quiet_hours: dict[str, Any] | None = None
    communication_style: str | None = Field(default=None, max_length=64)
    job_title: str | None = Field(default=None, max_length=200)
    company: str | None = Field(default=None, max_length=200)
    bio_summary: str | None = Field(default=None, max_length=2000)


class PreferencesOut(BaseModel):
    namespace: str
    values: dict[str, Any]


class PreferencesUpdate(BaseModel):
    values: dict[str, Any]


class PermissionsOut(BaseModel):
    role: str
    permissions: list[str]
