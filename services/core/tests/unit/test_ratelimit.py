"""Unit tests for the fixed-window rate limiter."""

from __future__ import annotations

import asyncio

import pytest
from zara.errors import AppError
from zara.services import ratelimit
from zara.services.ratelimit import RateLimiter, RateLimitResult


async def test_in_process_window_counts_and_then_limits() -> None:
    limiter = RateLimiter()

    first = await limiter.hit("key", limit=3, window_seconds=60)
    second = await limiter.hit("key", limit=3, window_seconds=60)
    third = await limiter.hit("key", limit=3, window_seconds=60)

    assert (first.remaining, second.remaining, third.remaining) == (2, 1, 0)
    with pytest.raises(AppError) as excinfo:
        await limiter.hit("key", limit=3, window_seconds=60)

    error = excinfo.value
    assert error.status_code == 429
    assert error.code == "rate_limited"
    headers = dict(error.headers or {})
    assert headers["RateLimit-Limit"] == "3"
    assert headers["RateLimit-Remaining"] == "0"
    assert headers["RateLimit-Reset"] == "60"
    assert int(headers["Retry-After"]) >= 1


async def test_windows_are_independent_per_key() -> None:
    limiter = RateLimiter()
    await limiter.hit("a", limit=1, window_seconds=60)
    other = await limiter.hit("b", limit=1, window_seconds=60)

    assert other.remaining == 0
    with pytest.raises(AppError):
        await limiter.hit("a", limit=1, window_seconds=60)


async def test_window_resets_after_elapsing() -> None:
    limiter = RateLimiter()
    await limiter.hit("k", limit=1, window_seconds=1)
    with pytest.raises(AppError):
        await limiter.hit("k", limit=1, window_seconds=1)

    await asyncio.sleep(1.05)

    again = await limiter.hit("k", limit=1, window_seconds=1)
    assert again.remaining == 0


async def test_rate_limit_result_headers_shape() -> None:
    result = RateLimitResult(limit=5, remaining=4, reset_after=30)

    assert result.headers == {
        "RateLimit-Limit": "5",
        "RateLimit-Remaining": "4",
        "RateLimit-Reset": "30",
    }


class _BrokenPipeline:
    async def __aenter__(self) -> _BrokenPipeline:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False

    def incr(self, key: str) -> _BrokenPipeline:
        return self

    def expire(self, key: str, ttl: int) -> _BrokenPipeline:
        return self

    async def execute(self) -> tuple[object, ...]:
        raise ConnectionError("redis is down")


class _BrokenRedis:
    def pipeline(self, transaction: bool) -> _BrokenPipeline:
        return _BrokenPipeline()


class _BrokenRedisFactory:
    @staticmethod
    def from_url(url: str, **kwargs: object) -> _BrokenRedis:
        return _BrokenRedis()


async def test_redis_failure_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ratelimit, "Redis", _BrokenRedisFactory)

    limiter = RateLimiter()
    with pytest.raises(AppError) as excinfo:
        await limiter.hit("k", limit=10, window_seconds=30, redis_url="redis://unreachable:6379/0")

    error = excinfo.value
    assert error.status_code == 429
    assert error.code == "rate_limited"
    headers = dict(error.headers or {})
    assert headers["RateLimit-Remaining"] == "0"
    assert headers["RateLimit-Reset"] == "30"
