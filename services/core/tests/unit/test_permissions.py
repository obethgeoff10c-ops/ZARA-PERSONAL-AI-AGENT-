"""Unit tests for role/scope authorization helpers."""

from __future__ import annotations

import uuid

import pytest
from zara.auth.principal import Principal
from zara.errors import AppError
from zara.models import Integration
from zara.services.permissions import (
    has_permission,
    permissions_for,
    require_any_role,
    require_integration_scope,
    require_permission,
)


def _principal(role: str) -> Principal:
    return Principal(
        tenant_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        role=role,
        email="user@example.com",
        display_name=None,
        auth_provider="local",
        external_auth_id="dev:user@example.com",
    )


def test_permissions_for_unknown_role_is_empty() -> None:
    assert permissions_for("ghost") == frozenset()
    assert has_permission(_principal("ghost"), "conversation:read") is False


def test_role_hierarchy_owner_admin_member() -> None:
    owner = permissions_for("owner")
    admin = permissions_for("admin")
    member = permissions_for("member")

    assert member < admin < owner
    assert "audit:read" in owner and "audit:read" in admin
    assert "audit:read" not in member
    assert "integration:manage" not in member
    assert "memory:delete" not in member


def test_require_permission_passes_when_granted() -> None:
    require_permission(_principal("owner"), "audit:read")
    require_permission(_principal("member"), "conversation:read")


def test_require_permission_raises_403_when_missing() -> None:
    with pytest.raises(AppError) as excinfo:
        require_permission(_principal("member"), "audit:read")

    assert excinfo.value.status_code == 403
    assert excinfo.value.code == "forbidden"


def test_require_any_role_accepts_listed_role() -> None:
    require_any_role(_principal("admin"), {"owner", "admin"})


def test_require_any_role_rejects_other_role() -> None:
    with pytest.raises(AppError) as excinfo:
        require_any_role(_principal("member"), ["owner", "admin"])

    assert excinfo.value.status_code == 403


def _integration(scopes: list[str]) -> Integration:
    return Integration(
        provider="gmail",
        account_identifier="user@example.com",
        status="active",
        granted_scopes=scopes,
    )


def test_require_integration_scope_granted() -> None:
    require_integration_scope(_integration(["gmail.readonly"]), "gmail.readonly")


def test_require_integration_scope_missing() -> None:
    with pytest.raises(AppError) as excinfo:
        require_integration_scope(_integration(["calendar.readonly"]), "gmail.readonly")

    assert excinfo.value.status_code == 403
    assert "gmail.readonly" in excinfo.value.detail
