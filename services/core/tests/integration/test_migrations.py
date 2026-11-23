"""Migration/schema invariants against the live database (read-only)."""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from zara import models  # noqa: F401 - populate the metadata registry
from zara.config import get_settings
from zara.db.base import Base
from zara.db.engine import create_engine

pytestmark = pytest.mark.integration


def _head_revision() -> str:
    core_root = Path(__file__).resolve().parents[2]
    config = Config(str(core_root / "alembic.ini"))
    config.set_main_option("script_location", str(core_root / "alembic"))
    return ScriptDirectory.from_config(config).get_current_head()


async def test_alembic_version_is_at_script_head() -> None:
    engine = create_engine(get_settings())
    try:
        async with engine.connect() as connection:
            version = (
                await connection.execute(text("SELECT version_num FROM core.alembic_version"))
            ).scalar_one()
    finally:
        await engine.dispose()

    assert version == _head_revision()


async def test_every_model_table_exists_in_its_schema() -> None:
    engine = create_engine(get_settings())
    try:
        async with engine.connect() as connection:
            rows = await connection.execute(
                text(
                    "SELECT table_schema, table_name FROM information_schema.tables"
                    " WHERE table_type = 'BASE TABLE'"
                )
            )
            present = {(row[0], row[1]) for row in rows}
    finally:
        await engine.dispose()

    missing = []
    for table in Base.metadata.tables.values():
        key = (table.schema or "public", table.name)
        if key not in present:
            missing.append(f"{key[0]}.{key[1]}")

    assert not missing


async def test_tenant_scoped_tables_enforce_row_level_security() -> None:
    tenant_tables = sorted(
        f"{table.schema}.{table.name}"
        for table in Base.metadata.tables.values()
        if "tenant_id" in table.columns
    )
    # ``core.tenants`` has no tenant_id column but is still RLS-protected.
    tenant_tables.append("core.tenants")
    assert tenant_tables

    engine = create_engine(get_settings())
    try:
        async with engine.connect() as connection:
            rows = await connection.execute(
                text(
                    """
                    SELECT n.nspname || '.' || c.relname, c.relrowsecurity, c.relforcerowsecurity
                    FROM pg_class c
                    JOIN pg_namespace n ON n.oid = c.relnamespace
                    WHERE c.relkind = 'r'
                    """
                )
            )
            flags = {row[0]: (row[1], row[2]) for row in rows}
    finally:
        await engine.dispose()

    unprotected = [name for name in tenant_tables if flags.get(name) != (True, True)]
    assert not unprotected


async def _assert_audit_mutation_rejected(statement: str) -> str:
    engine = create_engine(get_settings())
    caught: SQLAlchemyError | None = None
    try:
        async with engine.begin() as connection:
            # The API role has no UPDATE/DELETE grant, so Postgres refuses before
            # the append-only trigger is reached; either way the write must fail.
            await connection.execute(text("SET LOCAL ROLE zara_app"))
            with pytest.raises(SQLAlchemyError) as excinfo:
                await connection.execute(text(statement))
        caught = excinfo.value
    finally:
        await engine.dispose()

    assert caught is not None
    return str(caught)


@pytest.mark.parametrize(
    "statement", ["UPDATE audit.audit_log SET action = action", "DELETE FROM audit.audit_log"]
)
async def test_audit_log_rejects_mutation_as_application_role(statement: str) -> None:
    message = await _assert_audit_mutation_rejected(statement)
    # No API role holds UPDATE/DELETE on the audit table; the write must fail.
    assert "permission denied" in message or "append-only" in message


async def test_audit_log_append_only_trigger_fires_for_privileged_session() -> None:
    """A role with UPDATE privilege is still stopped by the database trigger.

    The probe row is inserted through ``zara_audit_writer`` and rolled back with
    the surrounding transaction, so the tenant's hash chain is never touched.
    """
    engine = create_engine(get_settings())
    tenant_id = str(uuid.uuid4())
    row_id = uuid.uuid4()
    caught: SQLAlchemyError | None = None
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text("SELECT set_config('app.tenant_id', :tenant_id, true)"),
                {"tenant_id": tenant_id},
            )
            # A real tenant row is required by the foreign key; everything in this
            # transaction rolls back after the probe.
            await connection.execute(
                text("INSERT INTO core.tenants (id, name, slug) VALUES (:id, 'probe', :slug)"),
                {"id": tenant_id, "slug": f"probe-{tenant_id}"},
            )
            await connection.execute(text("SET LOCAL ROLE zara_audit_writer"))
            await connection.execute(
                text(
                    "INSERT INTO audit.audit_log (id, tenant_id, actor_type, action, row_hash)"
                    " VALUES (:id, :tenant_id, 'user', 'test.probe', :row_hash)"
                ),
                {"id": row_id, "tenant_id": tenant_id, "row_hash": b"\x00"},
            )
            await connection.execute(text("RESET ROLE"))
            with pytest.raises(SQLAlchemyError) as excinfo:
                await connection.execute(text("UPDATE audit.audit_log SET action = action"))
        caught = excinfo.value
    finally:
        await engine.dispose()

    assert caught is not None
    assert "append-only" in str(caught)
