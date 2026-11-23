"""Async engine lifecycle."""

from __future__ import annotations

import logging
from functools import lru_cache

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from zara.config import Settings, get_settings

logger = logging.getLogger(__name__)

_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None


def create_engine(settings: Settings | None = None) -> AsyncEngine:
    resolved = settings or get_settings()
    return create_async_engine(
        resolved.database_url,
        pool_pre_ping=True,
        pool_size=resolved.database_pool_size,
        max_overflow=resolved.database_max_overflow,
        pool_recycle=resolved.database_pool_recycle_seconds,
        connect_args={
            "timeout": resolved.database_connect_timeout_seconds,
            "server_settings": {
                "application_name": resolved.service_name,
                "statement_timeout": str(resolved.database_statement_timeout_ms),
            },
        },
    )


def get_engine(settings: Settings | None = None) -> AsyncEngine:
    """Return the process-wide engine, creating it on first use."""
    global _engine, _sessionmaker
    if _engine is None:
        _engine = create_engine(settings)
        _sessionmaker = async_sessionmaker(_engine, expire_on_commit=False, autoflush=False)
        logger.info(
            "database engine created",
            extra={"event": "db.engine.created"},
        )
    return _engine


def get_sessionmaker(settings: Settings | None = None) -> async_sessionmaker[AsyncSession]:
    if _sessionmaker is None:
        get_engine(settings)
    assert _sessionmaker is not None
    return _sessionmaker


async def dispose_engine() -> None:
    """Dispose pooled connections (called from the app lifespan and tests)."""
    global _engine, _sessionmaker
    if _engine is not None:
        await _engine.dispose()
        logger.info("database engine disposed", extra={"event": "db.engine.disposed"})
    _engine = None
    _sessionmaker = None


@lru_cache(maxsize=1)
def engine_is_configured() -> bool:
    return True
