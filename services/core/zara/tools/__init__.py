"""Centralized tool registry.

Importing this package registers every built-in tool and runs boot validation.
Startup fails (import error) on any catalogue inconsistency, so a bad tool spec
can never reach production.
"""

from __future__ import annotations

from zara.services.oauth import PROVIDERS
from zara.tools.builtin import BUILTIN_SPECS
from zara.tools.preview import PREVIEWS
from zara.tools.registry import DuplicateToolError, ToolRegistry
from zara.tools.spec import ApprovalPolicy, ToolSpec

_EXPECTED_APPROVAL = {
    0: ApprovalPolicy.AUTO,
    1: ApprovalPolicy.AUTO,
    2: ApprovalPolicy.AUTO,
    3: ApprovalPolicy.APPROVAL,
    4: ApprovalPolicy.ALWAYS,
}


class ToolCatalogueError(RuntimeError):
    pass


def validate_catalogue(items: list[ToolSpec]) -> None:
    names: set[str] = set()
    for spec in items:
        if spec.name in names:
            raise ToolCatalogueError(f"duplicate tool name: {spec.name}")
        names.add(spec.name)
        if spec.preview_renderer and spec.preview_renderer not in PREVIEWS:
            raise ToolCatalogueError(
                f"{spec.name}: unknown preview renderer {spec.preview_renderer!r}"
            )
        if _EXPECTED_APPROVAL[spec.risk_tier] == ApprovalPolicy.AUTO:
            if spec.approval != ApprovalPolicy.AUTO:
                raise ToolCatalogueError(
                    f"{spec.name}: tier {spec.risk_tier} may not be stricter than auto"
                )
        else:
            if spec.approval != _EXPECTED_APPROVAL[spec.risk_tier]:
                raise ToolCatalogueError(
                    f"{spec.name}: tier {spec.risk_tier} requires "
                    f"{_EXPECTED_APPROVAL[spec.risk_tier]}"
                )
        if spec.risk_tier >= 3 and not spec.critical_args:
            raise ToolCatalogueError(f"{spec.name}: tier >= 3 tools must declare critical_args")


_IDENTITY_SCOPES = {"openid", "email", "profile"}


def validate_provider_scopes(items: list[ToolSpec]) -> None:
    """Every tool scope was requested, and every requested data scope is used."""
    used: dict[str, set[str]] = {}
    for spec in items:
        if spec.integration is None:
            continue
        requested = set(PROVIDERS[spec.integration].scopes)
        missing = set(spec.required_scopes) - requested
        if missing:
            raise ToolCatalogueError(
                f"{spec.name}: scopes not requested by {spec.integration}: {sorted(missing)}"
            )
        used.setdefault(spec.integration, set()).update(spec.required_scopes)
    for provider, config in PROVIDERS.items():
        unused = set(config.scopes) - _IDENTITY_SCOPES - used.get(provider, set())
        if unused:
            raise ToolCatalogueError(
                f"{provider}: requested scopes with no shipped tool: {sorted(unused)}"
            )


def build_registry() -> ToolRegistry:
    target = ToolRegistry()
    try:
        target.register_all(BUILTIN_SPECS)
    except DuplicateToolError as exc:  # pragma: no cover - defensive
        raise ToolCatalogueError(str(exc)) from exc
    validate_catalogue(target.all())
    validate_provider_scopes(target.all())
    return target


registry = build_registry()

__all__ = [
    "DuplicateToolError",
    "ToolCatalogueError",
    "ToolRegistry",
    "build_registry",
    "registry",
    "validate_catalogue",
]
