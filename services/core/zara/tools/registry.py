"""Static tool registry (05-TOOLS.md §1)."""

from __future__ import annotations

from collections.abc import Iterable

from zara.tools.spec import ToolSpec


class DuplicateToolError(ValueError):
    pass


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, ToolSpec] = {}

    def register(self, spec: ToolSpec) -> None:
        if spec.name in self._tools:
            raise DuplicateToolError(f"duplicate tool name: {spec.name}")
        self._tools[spec.name] = spec

    def register_all(self, specs: Iterable[ToolSpec]) -> None:
        for spec in specs:
            self.register(spec)

    def get(self, name: str) -> ToolSpec | None:
        return self._tools.get(name)

    def require(self, name: str) -> ToolSpec:
        spec = self.get(name)
        if spec is None:
            raise KeyError(name)
        return spec

    def all(self) -> list[ToolSpec]:
        return sorted(self._tools.values(), key=lambda item: item.name)

    def expose(self, domains: Iterable[str]) -> list[ToolSpec]:
        wanted = set(domains)
        return [spec for spec in self.all() if "CORE" in spec.domains or spec.domains & wanted]


registry = ToolRegistry()
