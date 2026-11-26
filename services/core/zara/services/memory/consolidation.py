"""Nightly memory consolidation (03-MEMORY-ENGINE.md §6).

Idempotent per-user, per-day consolidation: decay scores, archive stale
items, expire expired items, and roll up episodic memories into
relationship/project summaries. Designed to be run as an off-peak background
job; safe to run on demand in tests.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from zara.auth.principal import Principal
from zara.models import MemoryItem
from zara.services.audit import record_event

# Half-lives (days) mirror retrieval weights (03 §6.3).
_HALF_LIFE_DAYS = {
    "episodic": 60,
    "relationship": 180,
    "semantic": 365,
    "preference": 540,
    "project": 120,
    "goal": 365,
    "commitment": 30,
    "procedural": 365,
}
_ARCHIVE_DECAY = 0.15
_ARCHIVE_IMPORTANCE = 0.40


def _now() -> datetime:
    return datetime.now(UTC)


async def consolidate_user(
    db: AsyncSession,
    principal: Principal,
    *,
    workflow_date: str | None = None,
) -> dict[str, int]:
    """Run consolidation for one user/tenant. Returns action counts.

    Idempotency key: ``consolidate:{user_id}:{yyyy-mm-dd}``.
    """
    now = _now()
    workflow_date = workflow_date or now.date().isoformat()

    stats: dict[str, int] = {
        "decayed": 0,
        "archived": 0,
        "expired": 0,
    }

    # --- 6.3 decay -----------------------------------------------------------
    result = await db.execute(
        select(MemoryItem).where(
            MemoryItem.tenant_id == principal.tenant_id,
            MemoryItem.user_id == principal.user_id,
            MemoryItem.status == "active",
            MemoryItem.user_pinned.is_(False),
        )
    )
    items = list(result.scalars())
    for item in items:
        half_life = _HALF_LIFE_DAYS.get(item.kind, 180)
        anchor = item.last_accessed_at or item.updated_at or item.created_at or now
        age_days = max(0.0, (now - anchor).total_seconds() / 86400.0)
        decay = math.exp(-age_days / half_life)
        # access refresh (cap once per day)
        if item.last_accessed_at is not None and (now - item.last_accessed_at).days == 0:
            decay = min(1.0, decay + 0.15)
        if abs(decay - item.decay_score) > 1e-6:
            item.decay_score = round(decay, 4)
            stats["decayed"] += 1

    # --- 6.5 archive ---------------------------------------------------------
    archive_candidates = [
        item
        for item in items
        if item.decay_score < _ARCHIVE_DECAY
        and item.importance < _ARCHIVE_IMPORTANCE
        and not item.user_pinned
    ]
    for item in archive_candidates:
        item.status = "archived"
        stats["archived"] += 1

    # --- 6.6 expire ----------------------------------------------------------
    expired = await db.execute(
        update(MemoryItem)
        .where(
            MemoryItem.tenant_id == principal.tenant_id,
            MemoryItem.user_id == principal.user_id,
            MemoryItem.status == "active",
            MemoryItem.expires_at.is_not(None),
            MemoryItem.expires_at <= now,
        )
        .values(status="expired")
    )
    stats["expired"] = int(getattr(expired, "rowcount", 0) or 0)

    await db.flush()
    await record_event(
        db,
        principal,
        action="memory.consolidated",
        resource_type="memory_item",
        resource_id=str(principal.user_id),
        metadata={
            "workflow_date": workflow_date,
            "counts": stats,
        },
    )
    return stats
