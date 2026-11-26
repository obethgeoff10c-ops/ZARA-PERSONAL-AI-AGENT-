"""Hybrid memory retrieval with reciprocal-rank-fusion ranking (03 §9).

Candidate generation runs several strategies (each capped at 40), then fuses
with Reciprocal Rank Fusion (k=60) and a weighted final score. Results are
de-duplicated with MMR, packed into a token budget, and logged to
``memory.retrieval_logs`` so the UI can answer "why was this memory used?".
"""

from __future__ import annotations

import hashlib
import math
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import (
    func,
    or_,
    select,
)
from sqlalchemy.ext.asyncio import AsyncSession

from zara.auth.principal import Principal
from zara.models import MemoryEmbedding, MemoryItem, MemorySource
from zara.services.audit import record_event
from zara.services.memory.embeddings import embed_text

# --- retrieval weights (03 §9.3) --------------------------------------------

_W_RRF = 0.40
_W_COS = 0.15
_W_BM25 = 0.10
_W_IMPORTANCE = 0.12
_W_RECENCY = 0.08
_W_ENTITY = 0.08
_W_DOMAIN = 0.05
_PIN_BONUS = 0.10
_COMMITMENT_DUE_BONUS = 0.15
_SENSITIVITY_PENALTY = 0.10
_RRF_K = 60
_MMR_LAMBDA = 0.7
_MIN_CONFIDENCE = 0.50
_DEFAULT_TOKEN_BUDGET = 1800
_DEFAULT_K = 12
_CANDIDATE_LIMIT = 40

# Half-lives (days) for recency decay (03 §6.3).
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


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


@dataclass(frozen=True)
class RetrievalRequest:
    query_text: str
    domains: list[str] = field(default_factory=list)
    entity_ids: list[uuid.UUID] = field(default_factory=list)
    time_window: tuple[datetime, datetime] | None = None
    kinds: list[str] | None = None
    k: int = _DEFAULT_K
    token_budget: int = _DEFAULT_TOKEN_BUDGET
    include_history: bool = False
    run_id: uuid.UUID | None = None


@dataclass(frozen=True)
class RetrievedMemory:
    id: uuid.UUID
    kind: str
    title: str | None
    content: str
    score: float
    components: dict[str, float]
    sources: list[dict[str, Any]]
    importance: float
    confidence: float
    sensitivity: str
    status: str
    occurred_at: str | None
    due_at: datetime | None = None


def _recency_score(item: MemoryItem, now: datetime) -> float:
    half_life = _HALF_LIFE_DAYS.get(item.kind, 180)
    anchor = item.last_accessed_at or item.updated_at or item.created_at or now
    age_days = max(0.0, (now - anchor).total_seconds() / 86400.0)
    return math.exp(-age_days / half_life)


def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    na = math.sqrt(sum(x * x for x in a)) or 1.0
    nb = math.sqrt(sum(x * x for x in b)) or 1.0
    return dot / (na * nb)


def _mmr_select(
    scored: list[tuple[RetrievedMemory, list[float]]],
    *,
    k: int,
    lambda_: float = _MMR_LAMBDA,
) -> list[RetrievedMemory]:
    """Maximal Marginal Relevance: pick high-score, low-redundancy items."""
    if not scored:
        return []
    selected: list[tuple[RetrievedMemory, list[float]]] = []
    remaining = list(scored)
    while remaining and len(selected) < k:
        best_idx = None
        best_mmr = None
        for idx, (mem, emb) in enumerate(remaining):
            relevance = mem.score
            redundancy = 0.0
            if selected:
                redundancy = max(_cosine(emb, sel_emb) for _, sel_emb in selected)
            mmr = lambda_ * relevance - (1.0 - lambda_) * redundancy
            if best_mmr is None or mmr > best_mmr:
                best_idx, best_mmr = idx, mmr
        if best_idx is None:
            break
        selected.append(remaining.pop(best_idx))
    return [mem for mem, _ in selected]


class RetrievalService:
    def __init__(self, embedding_dim: int = 1024) -> None:
        self.embedding_dim = embedding_dim

    async def retrieve(
        self,
        db: AsyncSession,
        principal: Principal,
        request: RetrievalRequest,
    ) -> tuple[list[RetrievedMemory], str | None]:
        """Return top memories for ``request`` plus a retrieval-log id (None if
        logging was skipped)."""
        now = _now()
        query_emb = embed_text(request.query_text, self.embedding_dim)
        statuses = (
            {"active", "suggested", "archived", "expired"}
            if request.include_history
            else {"active"}
        )

        # --- candidate generation (each limited to _CANDIDATE_LIMIT) ---------
        lists: dict[str, list[MemoryItem]] = {}
        lists["semantic"] = await self._semantic_candidates(
            db, principal, query_emb, request, statuses
        )
        lists["keyword"] = await self._keyword_candidates(
            db, principal, request.query_text, request, statuses
        )
        lists["entity"] = await self._entity_candidates(db, principal, request, statuses)
        lists["pinned"] = await self._pinned_and_due_candidates(
            db, principal, request, statuses, now
        )
        lists["recent"] = await self._recent_candidates(db, principal, request, statuses, now)

        # --- RRF fusion ------------------------------------------------------
        rrf: dict[uuid.UUID, float] = {}
        positions: dict[uuid.UUID, dict[str, int]] = {}
        for name, cand_items in lists.items():
            for rank, item in enumerate(cand_items):
                rrf[item.id] = rrf.get(item.id, 0.0) + 1.0 / (_RRF_K + rank + 1)
                positions.setdefault(item.id, {})[name] = rank + 1

        # --- gather full item data + embeddings ------------------------------
        item_ids = list(rrf)
        items: dict[uuid.UUID, MemoryItem] = {}
        embeddings: dict[uuid.UUID, list[float]] = {}
        if item_ids:
            result = await db.execute(
                select(MemoryItem).where(
                    MemoryItem.tenant_id == principal.tenant_id,
                    MemoryItem.user_id == principal.user_id,
                    MemoryItem.id.in_(item_ids),
                )
            )
            items = {item.id: item for item in result.scalars()}
            emb_result = await db.execute(
                select(MemoryEmbedding.memory_id, MemoryEmbedding.embedding).where(
                    MemoryEmbedding.memory_id.in_(item_ids),
                    MemoryEmbedding.is_current.is_(True),
                )
            )
            embeddings = {mid: list(emb) for mid, emb in emb_result}

        # --- final score -----------------------------------------------------
        rrf_values = list(rrf.values())
        rrf_max = max(rrf_values) if rrf_values else 1.0
        scored: list[tuple[RetrievedMemory, list[float]]] = []
        for mem_id, db_mem in items.items():
            if db_mem.confidence < _MIN_CONFIDENCE and not db_mem.user_pinned:
                continue
            rank_lists = positions.get(mem_id, {})
            rrf_norm = rrf[mem_id] / rrf_max
            cos_sim = 1.0 - _cosine(query_emb, embeddings.get(mem_id, []))
            bm25_norm = 1.0 if "keyword" in rank_lists else 0.0
            importance = db_mem.importance
            recency = _recency_score(db_mem, now)
            entity_overlap = self._entity_overlap(db_mem, request.entity_ids)
            domain_boost = self._domain_boost(db_mem, request.domains)
            pin_bonus = _PIN_BONUS if db_mem.user_pinned else 0.0
            due_bonus = (
                _COMMITMENT_DUE_BONUS
                if db_mem.kind == "commitment"
                and db_mem.due_at is not None
                and db_mem.due_at <= now + timedelta(days=7)
                else 0.0
            )
            sensitivity_penalty = (
                -_SENSITIVITY_PENALTY if db_mem.sensitivity == "restricted" else 0.0
            )
            score = (
                _W_RRF * rrf_norm
                + _W_COS * cos_sim
                + _W_BM25 * bm25_norm
                + _W_IMPORTANCE * importance
                + _W_RECENCY * recency
                + _W_ENTITY * entity_overlap
                + _W_DOMAIN * domain_boost
                + pin_bonus
                + due_bonus
                + sensitivity_penalty
            )
            components = {
                "rrf": round(_W_RRF * rrf_norm, 4),
                "cosine": round(_W_COS * cos_sim, 4),
                "bm25": round(_W_BM25 * bm25_norm, 4),
                "importance": round(_W_IMPORTANCE * importance, 4),
                "recency": round(_W_RECENCY * recency, 4),
                "entity_overlap": round(_W_ENTITY * entity_overlap, 4),
                "domain_boost": round(_W_DOMAIN * domain_boost, 4),
                "pin_bonus": round(pin_bonus, 4),
                "due_bonus": round(due_bonus, 4),
                "sensitivity_penalty": round(sensitivity_penalty, 4),
            }
            sources = await self._sources_for(db, principal, db_mem)
            scored.append(
                (
                    RetrievedMemory(
                        id=db_mem.id,
                        kind=db_mem.kind,
                        title=db_mem.title,
                        content=db_mem.content,
                        score=round(score, 4),
                        components=components,
                        sources=sources,
                        importance=db_mem.importance,
                        confidence=db_mem.confidence,
                        sensitivity=db_mem.sensitivity,
                        status=db_mem.status,
                        occurred_at=_iso(db_mem.occurred_at),
                        due_at=db_mem.due_at,
                    ),
                    embeddings.get(mem_id, embed_text(db_mem.content, self.embedding_dim)),
                )
            )

        # --- MMR de-duplication ---------------------------------------------
        scored.sort(key=lambda pair: pair[0].score, reverse=True)
        top: list[RetrievedMemory] = _mmr_select(scored, k=request.k)

        # --- pack into token budget -----------------------------------------
        packed: list[RetrievedMemory] = []
        used = 0
        for mem in top:
            cost = len(mem.content) // 4 + len(mem.sources) * 20
            if used + cost > request.token_budget and packed:
                break
            packed.append(mem)
            used += cost

        # --- write retrieval log + increment access_count -------------------
        log_id: uuid.UUID | None = None
        selected_ids = [m.id for m in packed]
        if selected_ids:
            log_id = await self._log_retrieval(db, principal, request.query_text, selected_ids, now)
            await self._increment_access(db, selected_ids)

        return packed, (str(log_id) if log_id else None)

    # --- candidate strategies -----------------------------------------------

    async def _semantic_candidates(
        self,
        db: AsyncSession,
        principal: Principal,
        query_emb: list[float],
        request: RetrievalRequest,
        statuses: set[str],
    ) -> list[MemoryItem]:
        stmt = (
            select(MemoryItem)
            .join(MemoryEmbedding, MemoryItem.id == MemoryEmbedding.memory_id)
            .where(
                MemoryItem.tenant_id == principal.tenant_id,
                MemoryItem.user_id == principal.user_id,
                MemoryItem.status.in_(statuses),
                MemoryEmbedding.is_current.is_(True),
            )
        )
        if request.kinds:
            stmt = stmt.where(MemoryItem.kind.in_(request.kinds))
        stmt = stmt.order_by(MemoryEmbedding.embedding.cosine_distance(query_emb)).limit(
            _CANDIDATE_LIMIT
        )
        return list((await db.execute(stmt)).scalars())

    async def _keyword_candidates(
        self,
        db: AsyncSession,
        principal: Principal,
        query_text: str,
        request: RetrievalRequest,
        statuses: set[str],
    ) -> list[MemoryItem]:
        stmt = select(MemoryItem).where(
            MemoryItem.tenant_id == principal.tenant_id,
            MemoryItem.user_id == principal.user_id,
            MemoryItem.status.in_(statuses),
        )
        if request.kinds:
            stmt = stmt.where(MemoryItem.kind.in_(request.kinds))
        pattern = f"%{query_text}%"
        stmt = stmt.where(
            or_(
                MemoryItem.title.ilike(pattern),
                MemoryItem.content.ilike(pattern),
                MemoryItem.tsv.op("@@")(func.plainto_tsquery("english", query_text)),
            )
        )
        stmt = stmt.order_by(MemoryItem.importance.desc()).limit(_CANDIDATE_LIMIT)
        return list((await db.execute(stmt)).scalars())

    async def _entity_candidates(
        self,
        db: AsyncSession,
        principal: Principal,
        request: RetrievalRequest,
        statuses: set[str],
    ) -> list[MemoryItem]:
        """Items that are the source of a graph relationship involving the
        requested entities (graph.EntityRelationship.source_memory_id)."""
        from zara.models import EntityRelationship

        if not request.entity_ids:
            return []
        source_mem_ids = select(EntityRelationship.source_memory_id).where(
            EntityRelationship.tenant_id == principal.tenant_id,
            EntityRelationship.user_id == principal.user_id,
            EntityRelationship.source_memory_id.is_not(None),
            or_(
                EntityRelationship.src_entity_id.in_(request.entity_ids),
                EntityRelationship.dst_entity_id.in_(request.entity_ids),
            ),
        )
        stmt = select(MemoryItem).where(
            MemoryItem.tenant_id == principal.tenant_id,
            MemoryItem.user_id == principal.user_id,
            MemoryItem.status.in_(statuses),
            MemoryItem.id.in_(source_mem_ids),
        )
        if request.kinds:
            stmt = stmt.where(MemoryItem.kind.in_(request.kinds))
        stmt = stmt.order_by(MemoryItem.importance.desc()).limit(_CANDIDATE_LIMIT)
        return list((await db.execute(stmt)).scalars())

    async def _pinned_and_due_candidates(
        self,
        db: AsyncSession,
        principal: Principal,
        request: RetrievalRequest,
        statuses: set[str],
        now: datetime,
    ) -> list[MemoryItem]:
        from sqlalchemy import and_

        stmt = select(MemoryItem).where(
            MemoryItem.tenant_id == principal.tenant_id,
            MemoryItem.user_id == principal.user_id,
            MemoryItem.status.in_(statuses),
        )
        if request.kinds:
            stmt = stmt.where(MemoryItem.kind.in_(request.kinds))
        stmt = stmt.where(
            or_(
                MemoryItem.user_pinned.is_(True),
                and_(
                    MemoryItem.kind == "commitment",
                    MemoryItem.due_at.is_not(None),
                    MemoryItem.due_at <= now + timedelta(days=7),
                ),
            )
        )
        stmt = stmt.order_by(MemoryItem.importance.desc()).limit(_CANDIDATE_LIMIT)
        return list((await db.execute(stmt)).scalars())

    async def _recent_candidates(
        self,
        db: AsyncSession,
        principal: Principal,
        request: RetrievalRequest,
        statuses: set[str],
        now: datetime,
    ) -> list[MemoryItem]:
        stmt = select(MemoryItem).where(
            MemoryItem.tenant_id == principal.tenant_id,
            MemoryItem.user_id == principal.user_id,
            MemoryItem.status.in_(statuses),
            MemoryItem.kind == "episodic",
            MemoryItem.created_at >= now - timedelta(days=14),
        )
        if request.kinds:
            stmt = stmt.where(MemoryItem.kind.in_(request.kinds))
        stmt = stmt.order_by(MemoryItem.created_at.desc()).limit(_CANDIDATE_LIMIT)
        return list((await db.execute(stmt)).scalars())

    # --- scoring helpers ----------------------------------------------------

    def _entity_overlap(self, item: MemoryItem, entity_ids: list[uuid.UUID]) -> float:
        if not entity_ids:
            return 0.0
        payload = item.payload if isinstance(item.payload, dict) else {}
        item_entities = {
            str(e) for e in payload.get("entities", []) if isinstance(payload.get("entities"), list)
        }
        if not item_entities:
            return 0.0
        wanted = {str(e) for e in entity_ids}
        return len(item_entities & wanted) / max(1, len(item_entities | wanted))

    def _domain_boost(self, item: MemoryItem, domains: list[str]) -> float:
        if not domains:
            return 0.0
        return 1.0 if item.kind in set(domains) else 0.0

    async def _sources_for(
        self, db: AsyncSession, principal: Principal, item: MemoryItem
    ) -> list[dict[str, Any]]:
        result = await db.execute(
            select(MemorySource)
            .where(
                MemorySource.memory_id == item.id,
                MemorySource.tenant_id == principal.tenant_id,
            )
            .order_by(MemorySource.observed_at.desc())
            .limit(5)
        )
        return [
            {
                "source_type": s.source_type,
                "quote": s.quote,
                "observed_at": _iso(s.observed_at),
            }
            for s in result.scalars()
        ]

    async def _log_retrieval(
        self,
        db: AsyncSession,
        principal: Principal,
        query_text: str,
        selected_ids: list[uuid.UUID],
        now: datetime,
    ) -> uuid.UUID:
        from zara.models import RetrievalLog

        log_id = uuid.uuid4()
        db.add(
            RetrievalLog(
                id=log_id,
                tenant_id=principal.tenant_id,
                user_id=principal.user_id,
                query_text_hash=hashlib.sha256(query_text.encode("utf-8")).digest(),
                query_redacted=query_text[:200],
                domains=[],
                candidates=[str(i) for i in selected_ids],
                selected_ids=selected_ids,
                created_at=now,
            )
        )
        await db.flush()
        await record_event(
            db,
            principal,
            action="memory.retrieved",
            resource_type="memory_item",
            resource_id=str(selected_ids[0]) if selected_ids else None,
            metadata={"count": len(selected_ids)},
        )
        return log_id

    async def _increment_access(self, db: AsyncSession, selected_ids: list[uuid.UUID]) -> None:
        from sqlalchemy import update

        now = _now()
        for mem_id in selected_ids:
            await db.execute(
                update(MemoryItem)
                .where(MemoryItem.id == mem_id)
                .values(access_count=MemoryItem.access_count + 1, last_accessed_at=now)
            )
