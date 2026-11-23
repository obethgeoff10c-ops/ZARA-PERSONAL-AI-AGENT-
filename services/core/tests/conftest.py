"""Shared pytest configuration.

Unit tests never touch a database. Integration tests are opt-in via
``ZARA_INTEGRATION=1`` and a reachable ``DATABASE_URL``.
"""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault(
    "DATABASE_URL",
    "postgresql+asyncpg://zara:zara_local_dev@localhost:5432/zara_test",
)
os.environ.setdefault("ENVIRONMENT", "test")
os.environ.setdefault("LOG_LEVEL", "WARNING")
os.environ.setdefault("CORS_ORIGINS", "http://localhost:3000")
os.environ.setdefault("ALLOWED_HOSTS", "localhost,127.0.0.1,testserver")

from zara.api.app import create_app
from zara.config import Settings


@pytest.fixture
def settings() -> Settings:
    return Settings(
        _env_file=None,
        environment="test",
        database_url="postgresql+asyncpg://zara:zara_local_dev@localhost:5432/zara_test",
        cors_origins=["http://localhost:3000"],
        allowed_hosts=["localhost", "127.0.0.1", "testserver"],
        log_level="WARNING",
        redis_url=None,
    )


@pytest.fixture
def client(settings: Settings) -> Iterator[TestClient]:
    app = create_app(settings)
    with TestClient(app) as test_client:
        yield test_client
