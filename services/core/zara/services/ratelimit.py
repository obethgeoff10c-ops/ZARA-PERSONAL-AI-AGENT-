"""Fixed-window rate limiting with a Redis backend and an in-process fallback."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass

from redis.asyncio import Redis

from zara.errors import rate_limited


@dataclass(frozen=True, slots=True)
class RateLimitResult:
    limit: int
    remaining: int
    reset_after: int

    @property
    def headers(self) -> dict[str, str]:
        return {
            "RateLimit-Limit": str(self.limit),
            "RateLimit-Remaining": str(self.remaining),
            "RateLimit-Reset": str(self.reset_after),
        }


class RateLimiter:
    """Small, dependency-light limiter.

    Redis is used when configured (multi-process correctness); otherwise the
    process-local window still protects a single-node deployment.
    """

    def __init__(self) -> None:
        self._hits: dict[str, list[float]] = {}
        self._lock = asyncio.Lock()
        self._redis: Redis | None = None

    async def hit(
        self,
        key: str,
        *,
        limit: int,
        window_seconds: int = 60,
        redis_url: str | None = None,
    ) -> RateLimitResult:
        if redis_url:
            return await self._redis_hit(key, limit, window_seconds, redis_url)
        return await self._memory_hit(key, limit, window_seconds)

    async def _memory_hit(self, key: str, limit: int, window_seconds: int) -> RateLimitResult:
        now = time.monotonic()
        cutoff = now - window_seconds
        async with self._lock:
            bucket = [value for value in self._hits.get(key, []) if value > cutoff]
            allowed = len(bucket) < limit
            if allowed:
                bucket.append(now)
            self._hits[key] = bucket
            if len(self._hits) > 10_000:
                self._hits = {
                    item_key: values
                    for item_key, values in self._hits.items()
                    if values and values[-1] > cutoff
                }
            remaining = max(0, limit - len(bucket))
            reset_after = window_seconds
        if not allowed:
            raise rate_limited(
                "Too many requests. Wait before retrying.",
                retry_after=reset_after,
                headers=self._headers(limit, remaining, reset_after),
            )
        return RateLimitResult(limit=limit, remaining=remaining, reset_after=reset_after)

    async def _redis_hit(
        self, key: str, limit: int, window_seconds: int, redis_url: str
    ) -> RateLimitResult:
        try:
            if self._redis is None:
                self._redis = Redis.from_url(redis_url, decode_responses=True)
            redis_key = f"zara:rl:{key}:{int(time.time()) // window_seconds}"
            async with self._redis.pipeline(transaction=True) as pipe:
                pipe.incr(redis_key)
                pipe.expire(redis_key, window_seconds + 1)
                count, _ = await pipe.execute()
            remaining = max(0, limit - int(count))
            reset_after = window_seconds
            if int(count) > limit:
                raise rate_limited(
                    "Too many requests. Wait before retrying.",
                    retry_after=reset_after,
                    headers=self._headers(limit, remaining, reset_after),
                )
            return RateLimitResult(limit=limit, remaining=remaining, reset_after=reset_after)
        except Exception as exc:  # pragma: no cover - Redis outage path
            from zara.errors import AppError

            if isinstance(exc, AppError):
                raise
            # Fail closed for auth/sensitive routes: an unavailable limiter must
            # not silently disable abuse protection.
            raise rate_limited(
                "Rate limiting is temporarily unavailable. Retry shortly.",
                retry_after=window_seconds,
                headers=self._headers(limit, 0, window_seconds),
            ) from exc

    @staticmethod
    def _headers(limit: int, remaining: int, reset_after: int) -> dict[str, str]:
        return RateLimitResult(limit=limit, remaining=remaining, reset_after=reset_after).headers
