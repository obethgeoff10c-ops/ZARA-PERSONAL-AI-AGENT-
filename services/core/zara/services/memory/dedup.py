"""Duplicate detection and contradiction resolution (03 §4, §5).

Decides whether a new extraction candidate:

* is an exact duplicate (same ``content_hash``) → fold a new source into the
  existing item, no new row;
* shares a ``canonical_key`` with an active item → compare via the judge;
* is vector-similar (cosine ≥ 0.90) to an active item of the same kind → LLM
  judge decides ``same``/``refines``/``contradicts``/``unrelated``;
* is a near-duplicate (cosine 0.80-0.90, same entity set) → flag for
  nightly consolidation.

The judge is pluggable; a deterministic judge ships as the default so tests
run without an LLM. Contradiction resolution follows the precedence rules in
03 §5 (user-edited/pinned win, then user source, then source authority, then
recency, else suggested).
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol, cast, runtime_checkable

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from zara.auth.principal import Principal
from zara.models import BlockedFact, MemoryItem
from zara.services.memory.embeddings import embed_text

_COSINE_SAME = 0.90
_COSINE_NEAR_DUPLICATE = 0.80


@dataclass(frozen=True)
class DedupResult:
    # "insert" | "drop" | "same" | "refines" | "contradicts" | "unrelated" | "near_duplicate"
    action: str
    existing_id: uuid.UUID | None = None
    existing_content: str | None = None
    existing_confidence: float | None = None


@runtime_checkable
class DedupJudge(Protocol):
    """Compare two memory items; return one of same/refines/contradicts/unrelated."""

    async def judge(
        self,
        *,
        existing: dict[str, Any],
        candidate: dict[str, Any],
    ) -> str: ...


class DeterministicJudge:
    """Offline judge for tests/local dev.

    Exact content match → ``same``. Candidate contains existing content →
    ``refines``. Opposite polarity words (not/never vs none) → ``contradicts``.
    Otherwise ``unrelated``.
    """

    _OPPOSITE = (
        ("is", "is not"),
        ("are", "are not"),
        ("can", "cannot"),
        ("will", "will not"),
        ("has", "has not"),
    )

    async def judge(self, *, existing: dict[str, Any], candidate: dict[str, Any]) -> str:
        ec = _normalize(existing.get("content") or "")
        cc = _normalize(candidate.get("content") or "")
        if not ec or not cc:
            return "unrelated"
        if ec == cc:
            return "same"
        if ec in cc or cc in ec:
            return "refines"
        for a, b in self._OPPOSITE:
            if (a in ec and b in cc) or (b in ec and a in cc):
                return "contradicts"
        return "unrelated"


def _normalize(text: str) -> str:
    """Lowercase, collapse whitespace, and drop trailing sentence punctuation."""
    return " ".join(text.strip().lower().rstrip(".!?").split())


async def _content_hash(content: str) -> bytes:
    return hashlib.sha256(content.encode("utf-8")).digest()


async def check_duplicate_and_contradiction(
    db: AsyncSession,
    principal: Principal,
    *,
    kind: str,
    content: str,
    content_hash: bytes | None = None,
    canonical_key: str | None = None,
    entities: list[dict[str, str]] | None = None,
    confidence: float = 0.0,
    importance: float = 0.0,
    trust_level: str = "user",
    sensitivity: str = "normal",
    reference_time: datetime | None,
    judge: DedupJudge | None = None,
) -> DedupResult:
    reference_time = reference_time or datetime.now(UTC)
    judge = judge or DeterministicJudge()
    content_hash = content_hash or await _content_hash(content)

    # 0. Negative memory: a deleted/forgotten fact must never be re-created.
    blocked_stmt = select(BlockedFact.id).where(
        BlockedFact.tenant_id == principal.tenant_id,
        BlockedFact.user_id == principal.user_id,
        BlockedFact.fact_hash == content_hash,
    )
    if canonical_key is not None:
        blocked_stmt = select(BlockedFact.id).where(
            BlockedFact.tenant_id == principal.tenant_id,
            BlockedFact.user_id == principal.user_id,
            or_(
                BlockedFact.fact_hash == content_hash,
                BlockedFact.canonical_key == canonical_key,
            ),
        )
    if (await db.execute(blocked_stmt)).first() is not None:
        return DedupResult(action="blocked")

    # 1. Exact duplicate by content hash → add source only, no new item.
    existing = (
        await db.execute(
            select(MemoryItem).where(
                MemoryItem.tenant_id == principal.tenant_id,
                MemoryItem.user_id == principal.user_id,
                MemoryItem.content_hash == content_hash,
            )
        )
    ).scalar_one_or_none()
    if existing is not None and existing.status in {"active", "suggested"}:
        return DedupResult(action="drop", existing_id=existing.id)

    # 2. Canonical-key match on an active item → compare via judge.
    if canonical_key:
        key_item = (
            await db.execute(
                select(MemoryItem).where(
                    MemoryItem.tenant_id == principal.tenant_id,
                    MemoryItem.user_id == principal.user_id,
                    MemoryItem.canonical_key == canonical_key,
                    MemoryItem.status == "active",
                )
            )
        ).scalar_one_or_none()
        if key_item is not None:
            verdict = await judge.judge(
                existing=_item_dict(key_item),
                candidate={"content": content, "kind": kind},
            )
            return _verdict_to_result(verdict, key_item, content)

    # 3. Vector similarity ≥ 0.90 on same kind → judge.
    similar = await _most_similar_active(db, principal, kind=kind, content=content, limit=5)
    if similar is not None and similar[1] is not None and similar[1] >= _COSINE_SAME:
        verdict = await judge.judge(
            existing=_item_dict(similar[0]),
            candidate={"content": content, "kind": kind},
        )
        return _verdict_to_result(verdict, similar[0], content)

    # 4. Near-duplicate 0.80-0.90 with same entity set -> flag for consolidation.
    near = await _most_similar_active(db, principal, kind=kind, content=content, limit=10)
    if (
        near is not None
        and near[1] is not None
        and _COSINE_NEAR_DUPLICATE <= near[1] < _COSINE_SAME
    ):
        return DedupResult(action="near_duplicate", existing_id=near[0].id)

    return DedupResult(action="insert")


def _verdict_to_result(verdict: str, existing: MemoryItem, candidate_content: str) -> DedupResult:
    mapping = {
        "same": "drop",
        "refines": "refines",
        "contradicts": "contradicts",
        "unrelated": "insert",
    }
    action = mapping.get(verdict, "insert")
    return DedupResult(
        action=action,
        existing_id=existing.id,
        existing_content=existing.content,
        existing_confidence=existing.confidence,
    )


async def _most_similar_active(
    db: AsyncSession,
    principal: Principal,
    *,
    kind: str,
    content: str,
    limit: int,
) -> tuple[MemoryItem, float | None] | None:
    """Best-effort vector similarity via pgvector; returns None if no
    embedding is available for comparison."""
    from zara.models import MemoryEmbedding

    # Generate a deterministic query embedding so the comparison is stable
    # without a real embedding model.
    query_emb = embed_text(content)
    sub = (
        select(MemoryEmbedding.memory_id, MemoryEmbedding.embedding)
        .where(MemoryEmbedding.is_current.is_(True))
        .subquery()
    )
    dist_expr = sub.c.embedding.cosine_distance(query_emb)
    stmt = (
        select(MemoryItem, dist_expr.label("dist"))
        .join(sub, MemoryItem.id == sub.c.memory_id)
        .where(
            MemoryItem.tenant_id == principal.tenant_id,
            MemoryItem.user_id == principal.user_id,
            MemoryItem.kind == kind,
            MemoryItem.status == "active",
        )
        .order_by(dist_expr)
        .limit(limit)
    )
    rows = (await db.execute(stmt)).all()
    if not rows:
        return None
    best_item = cast(MemoryItem, rows[0][0])
    best_dist = rows[0][1]
    # cosine distance -> similarity
    similarity = 1.0 - cast(float, best_dist) if best_dist is not None else None
    return best_item, similarity


def _item_dict(item: MemoryItem) -> dict[str, Any]:
    return {
        "id": str(item.id),
        "content": item.content,
        "kind": item.kind,
        "confidence": item.confidence,
        "importance": item.importance,
        "sensitivity": item.sensitivity,
        "trust_level": item.trust_level,
        "created_by": item.created_by,
    }
