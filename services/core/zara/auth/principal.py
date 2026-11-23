"""Resolved request identity."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime


@dataclass(frozen=True, slots=True)
class Principal:
    """The authenticated caller, resolved from an IdP JWT or opaque session.

    ``tenant_id``/``user_id`` are server-derived and are the only source of
    truth for data access; client-supplied IDs are never trusted.
    """

    tenant_id: uuid.UUID
    user_id: uuid.UUID
    role: str
    email: str
    display_name: str | None
    auth_provider: str
    external_auth_id: str
    session_id: uuid.UUID | None = None
    authenticated_at: datetime | None = None
    mfa: bool = False
    scopes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def is_service(self) -> bool:
        return self.session_id is None and "service" in self.scopes
