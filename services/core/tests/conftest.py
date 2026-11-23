"""Shared pytest configuration.

Unit tests never touch a database. Integration tests are opt-in via
``ZARA_INTEGRATION=1`` and a reachable ``DATABASE_URL``.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

# Test defaults must never shadow real configuration: environment variables take
# precedence over the repo ``.env`` file in pydantic-settings, so injecting a
# placeholder DATABASE_URL here would silently override the developer's actual
# connection string and break every integration test. The URL therefore comes
# from the environment (CI) or from ``.env`` (local; see ``make bootstrap``).
os.environ.setdefault("ENVIRONMENT", "test")
os.environ.setdefault("LOG_LEVEL", "WARNING")
os.environ.setdefault("CORS_ORIGINS", "http://localhost:3000")
os.environ.setdefault("ALLOWED_HOSTS", "localhost,127.0.0.1,testserver")

_REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
if "DATABASE_URL" not in os.environ and not (_REPOSITORY_ROOT / ".env").is_file():
    raise RuntimeError(
        "DATABASE_URL is not configured. Run `make bootstrap` to create .env "
        "from .env.example, or export DATABASE_URL before running tests."
    )

from zara.api.app import create_app  # noqa: E402 - env defaults above must be set first
from zara.config import Settings, get_settings  # noqa: E402

from helpers import Api  # noqa: E402


@pytest.fixture
def settings() -> Settings:
    """Validated configuration from ``.env``/environment, isolated from Redis.

    The real database URL is inherited from ``get_settings()`` so tests hit the
    same PostgreSQL instance the application uses; only the rate-limiter backend
    is forced to the in-process window so tests never share state through Redis.
    """
    return get_settings().model_copy(update={"redis_url": None})


@pytest.fixture
def client(settings: Settings) -> Iterator[TestClient]:
    app = create_app(settings)
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def api(client: TestClient) -> Api:
    return Api(client)


@pytest.fixture
async def seed_row() -> AsyncIterator[Callable[..., Awaitable[None]]]:
    """Insert a real row as the database owner inside a tenant RLS context."""
    from zara.db.engine import create_engine

    engine = create_engine(get_settings())

    async def _seed(
        tenant_id: uuid.UUID, user_id: uuid.UUID, statement: str, params: dict[str, object]
    ) -> None:
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "SELECT set_config('app.tenant_id', :tenant_id, true),"
                    " set_config('app.user_id', :user_id, true),"
                    " set_config('app.role', 'service', true)"
                ),
                {"tenant_id": str(tenant_id), "user_id": str(user_id)},
            )
            await connection.execute(text(statement), params)

    yield _seed
    await engine.dispose()
