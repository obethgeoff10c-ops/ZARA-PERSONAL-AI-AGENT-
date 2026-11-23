"""Live-infrastructure tests.

Run with ``ZARA_INTEGRATION=1`` and a migrated database:

    ZARA_INTEGRATION=1 DATABASE_URL=... uv run pytest tests/integration
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import text
from zara.config import get_settings
from zara.db.engine import create_engine

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.getenv("ZARA_INTEGRATION") != "1",
        reason="set ZARA_INTEGRATION=1 to run against a live database",
    ),
]


async def test_database_accepts_queries() -> None:
    engine = create_engine(get_settings())
    try:
        async with engine.connect() as connection:
            assert (await connection.execute(text("SELECT 1"))).scalar_one() == 1
    finally:
        await engine.dispose()


async def test_pgvector_extension_is_installed() -> None:
    engine = create_engine(get_settings())
    try:
        async with engine.connect() as connection:
            version = (
                await connection.execute(
                    text("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
                )
            ).scalar_one_or_none()
    finally:
        await engine.dispose()

    assert version is not None, "pgvector is not installed"


async def test_expected_schemas_exist() -> None:
    expected = {
        "core",
        "integrations",
        "content",
        "memory",
        "graph",
        "agent",
        "events",
        "proactive",
        "work",
        "crm",
        "dev",
        "audit",
        "billing",
    }
    engine = create_engine(get_settings())
    try:
        async with engine.connect() as connection:
            rows = await connection.execute(
                text("SELECT schema_name FROM information_schema.schemata")
            )
            present = {row[0] for row in rows}
    finally:
        await engine.dispose()

    assert expected <= present


async def test_migrations_are_at_head() -> None:
    engine = create_engine(get_settings())
    try:
        async with engine.connect() as connection:
            version = (
                await connection.execute(text("SELECT version_num FROM core.alembic_version"))
            ).scalar_one()
    finally:
        await engine.dispose()

    assert version == _head_revision()


def _head_revision() -> str:
    """The revision Alembic considers head (never hardcode a revision here)."""
    core_root = Path(__file__).resolve().parents[2]
    config = Config(str(core_root / "alembic.ini"))
    # ``script_location`` in alembic.ini is relative to the CWD; pin it so this
    # works no matter where pytest was invoked from.
    config.set_main_option("script_location", str(core_root / "alembic"))
    return ScriptDirectory.from_config(config).get_current_head()
