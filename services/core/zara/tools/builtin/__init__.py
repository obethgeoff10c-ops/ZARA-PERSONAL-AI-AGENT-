"""Built-in tool catalogue, registered at import time (05-TOOLS.md §1)."""

from __future__ import annotations

from zara.tools.builtin.github import GITHUB_SPECS
from zara.tools.builtin.google import GOOGLE_SPECS

BUILTIN_SPECS = (*GOOGLE_SPECS, *GITHUB_SPECS)

__all__ = ["BUILTIN_SPECS", "GITHUB_SPECS", "GOOGLE_SPECS"]
