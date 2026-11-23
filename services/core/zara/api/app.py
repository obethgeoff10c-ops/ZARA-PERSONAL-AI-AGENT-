"""FastAPI application factory."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware

from zara import __version__
from zara.api import health
from zara.api.errors import register_exception_handlers
from zara.api.openapi import install_openapi
from zara.api.routers import (
    activity,
    approvals,
    auth,
    conversations,
    integrations,
    me,
    memory,
    notifications,
    runs,
    tasks,
)
from zara.config import Settings, get_settings
from zara.db.engine import dispose_engine
from zara.observability.logging import configure_logging
from zara.observability.middleware import RequestContextMiddleware
from zara.services.ratelimit import RateLimiter

logger = logging.getLogger(__name__)

_CORS_METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"]
_CORS_HEADERS = [
    "Authorization",
    "Content-Type",
    "Idempotency-Key",
    "If-Match",
    "Last-Event-ID",
    "X-Request-Id",
]
_CORS_EXPOSE_HEADERS = [
    "X-Request-Id",
    "RateLimit-Limit",
    "RateLimit-Remaining",
    "RateLimit-Reset",
    "Retry-After",
]


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the ASGI application. Import-safe: no network or DB work happens here."""
    resolved = settings or get_settings()
    configure_logging(resolved)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        logger.info(
            "api starting",
            extra={
                "event": "api.startup",
                "environment": resolved.environment,
                "database_target": resolved.safe_database_target,
            },
        )
        try:
            yield
        finally:
            await dispose_engine()
            logger.info("api stopped", extra={"event": "api.shutdown"})

    docs_enabled = resolved.openapi_enabled and resolved.environment != "production"
    app = FastAPI(
        title="ZARA API",
        version=__version__,
        summary="Persistent, proactive personal-intelligence agent",
        docs_url="/docs" if docs_enabled else None,
        redoc_url=None,
        openapi_url="/openapi.json" if docs_enabled else None,
        lifespan=lifespan,
    )
    app.state.settings = resolved
    app.state.rate_limiter = RateLimiter()

    # Middleware is applied bottom-up: request context must be outermost so every
    # response (including CORS and host rejections) carries a request ID.
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=resolved.allowed_hosts)
    if resolved.cors_origin_list:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=resolved.cors_origin_list,
            allow_credentials=True,
            allow_methods=_CORS_METHODS,
            allow_headers=_CORS_HEADERS,
            expose_headers=_CORS_EXPOSE_HEADERS,
            max_age=600,
        )
    app.add_middleware(RequestContextMiddleware)

    register_exception_handlers(app)
    install_openapi(app)
    app.include_router(health.router)

    prefix = resolved.api_v1_prefix
    for module in (
        auth,
        me,
        conversations,
        tasks,
        memory,
        runs,
        approvals,
        notifications,
        integrations,
        activity,
    ):
        app.include_router(module.router, prefix=prefix)

    logger.info(
        "api configured",
        extra={"event": "api.configured", "environment": resolved.environment},
    )
    return app
