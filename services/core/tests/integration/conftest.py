"""Integration-test collection policy: opt in with ``ZARA_INTEGRATION=1``."""

from __future__ import annotations

import os

import pytest


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    if os.getenv("ZARA_INTEGRATION") == "1":
        return
    skip = pytest.mark.skip(reason="set ZARA_INTEGRATION=1 to run against a live database")
    for item in items:
        if "integration" in item.keywords:
            item.add_marker(skip)
