"""Liveness and readiness endpoints.

``/health`` proves the process is alive and never touches dependencies.
``/ready`` proves the process can serve traffic: database first, Redis when
configured. Failure details are logged server-side and reduced to an exception
class name in the response.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Response
from pydantic import BaseModel
from redis.asyncio import Redis
from sqlalchemy import text

from zara import __version__
from zara.api.deps import get_app_settings
from zara.config import Settings
from zara.db.engine import get_engine

SettingsDep = Annotated[Settings, Depends(get_app_settings)]

logger = logging.getLogger(__name__)

router = APIRouter(tags=["system"])

CheckStatus = Literal["ok", "error"]


class HealthResponse(BaseModel):
    status: Literal["ok"]
    version: str
    environment: str


class CheckResult(BaseModel):
    status: CheckStatus
    latency_ms: float
    detail: str | None = None


class ReadinessResponse(BaseModel):
    status: Literal["ready", "not_ready"]
    version: str
    environment: str
    checks: dict[str, CheckResult]


def _elapsed_ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000, 2)


async def check_database(settings: Settings) -> CheckResult:
    started = time.perf_counter()
    try:
        async with asyncio.timeout(settings.database_connect_timeout_seconds):
            engine = get_engine(settings)
            async with engine.connect() as connection:
                await connection.execute(text("SELECT 1"))
    except Exception as exc:
        logger.warning("database readiness check failed", exc_info=True)
        return CheckResult(
            status="error", latency_ms=_elapsed_ms(started), detail=type(exc).__name__
        )
    return CheckResult(status="ok", latency_ms=_elapsed_ms(started))


async def check_redis(settings: Settings) -> CheckResult:
    started = time.perf_counter()
    redis_url = settings.redis_url_str
    if redis_url is None:
        return CheckResult(status="error", latency_ms=0.0, detail="not_configured")

    client = Redis.from_url(
        redis_url,
        socket_connect_timeout=settings.redis_connect_timeout_seconds,
        socket_timeout=settings.redis_connect_timeout_seconds,
    )
    try:
        async with asyncio.timeout(settings.redis_connect_timeout_seconds):
            await client.ping()
    except Exception as exc:
        logger.warning("redis readiness check failed", exc_info=True)
        return CheckResult(
            status="error", latency_ms=_elapsed_ms(started), detail=type(exc).__name__
        )
    finally:
        await client.aclose()
    return CheckResult(status="ok", latency_ms=_elapsed_ms(started))


@router.get("/health", response_model=HealthResponse, summary="Liveness probe")
async def health(settings: SettingsDep) -> HealthResponse:
    return HealthResponse(status="ok", version=__version__, environment=settings.environment)


@router.get(
    "/ready",
    response_model=ReadinessResponse,
    summary="Readiness probe",
    responses={503: {"model": ReadinessResponse, "description": "A dependency is unavailable"}},
)
async def readiness(
    response: Response,
    settings: SettingsDep,
) -> ReadinessResponse:
    checks: dict[str, CheckResult] = {"database": await check_database(settings)}
    if settings.redis_url is not None:
        checks["redis"] = await check_redis(settings)

    is_ready = all(check.status == "ok" for check in checks.values())
    if not is_ready:
        response.status_code = 503

    return ReadinessResponse(
        status="ready" if is_ready else "not_ready",
        version=__version__,
        environment=settings.environment,
        checks=checks,
    )
