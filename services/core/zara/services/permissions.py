"""Role- and scope-based authorization helpers."""

from __future__ import annotations

from collections.abc import Iterable

from zara.auth.principal import Principal
from zara.errors import forbidden
from zara.models import Integration

# Roles are coarse; object ownership is enforced by RLS + tenant/user predicates.
ROLE_PERMISSIONS: dict[str, frozenset[str]] = {
    "owner": frozenset(
        {
            "me:read",
            "me:write",
            "me:export",
            "me:delete",
            "session:manage",
            "member:manage",
            "conversation:read",
            "conversation:write",
            "message:write",
            "task:read",
            "task:write",
            "run:read",
            "run:create",
            "run:cancel",
            "memory:read",
            "memory:write",
            "memory:delete",
            "approval:read",
            "approval:decide",
            "notification:read",
            "notification:write",
            "integration:read",
            "integration:manage",
            "integration:connect",
            "audit:read",
        }
    ),
    "admin": frozenset(
        {
            "me:read",
            "me:write",
            "me:export",
            "session:manage",
            "member:manage",
            "conversation:read",
            "conversation:write",
            "message:write",
            "task:read",
            "task:write",
            "run:read",
            "run:create",
            "run:cancel",
            "memory:read",
            "memory:write",
            "memory:delete",
            "approval:read",
            "approval:decide",
            "notification:read",
            "notification:write",
            "integration:read",
            "integration:manage",
            "integration:connect",
            "audit:read",
        }
    ),
    "member": frozenset(
        {
            "me:read",
            "me:write",
            "session:manage",
            "conversation:read",
            "conversation:write",
            "message:write",
            "task:read",
            "task:write",
            "run:read",
            "run:create",
            "run:cancel",
            "memory:read",
            "memory:write",
            "approval:read",
            "approval:decide",
            "notification:read",
            "notification:write",
            "integration:read",
            "integration:connect",
        }
    ),
}

# Sync is only attempted when the provider grant covers the read scope.
SYNC_SCOPES: dict[str, str] = {
    "gmail": "gmail.readonly",
    "gcal": "calendar.readonly",
    "gdrive": "drive.file",
    "github": "repo:read",
}


def permissions_for(role: str) -> frozenset[str]:
    return ROLE_PERMISSIONS.get(role, frozenset())


def has_permission(principal: Principal, permission: str) -> bool:
    return permission in permissions_for(principal.role)


def require_permission(principal: Principal, permission: str) -> None:
    if not has_permission(principal, permission):
        raise forbidden(f"Your role ({principal.role}) cannot perform '{permission}'.")


def require_any_role(principal: Principal, roles: Iterable[str]) -> None:
    if principal.role not in set(roles):
        raise forbidden("Your role cannot perform this action.")


def require_integration_scope(integration: Integration, scope: str) -> None:
    if scope not in integration.granted_scopes:
        raise forbidden(
            f"The {integration.provider} integration is missing the required '{scope}' scope.",
            # A missing scope is a reconnect prompt, not an authorization defect.
        )
