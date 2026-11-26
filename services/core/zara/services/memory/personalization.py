"""User-context personalization (03-MEMORY-ENGINE.md Personalization).

Assembles a contextually-relevant, non-invasive snapshot of what ZARA knows
about the user: preferences, communication style, goals, current projects,
commitments, relevant people, and past decisions. Reads only the caller's own
tenant/user rows (RLS enforced).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from zara.auth.principal import Principal
from zara.models import UserProfile
from zara.services.memory.retrieval import (
    RetrievalRequest,
    RetrievalService,
    RetrievedMemory,
)

_COMMITMENT_DUE_WINDOW = timedelta(days=7)
_DEFAULT_CONTEXT = "current goals commitments preferences projects decisions"


def _now() -> datetime:
    return datetime.now(UTC)


class PersonalizationService:
    def __init__(self, retrieval: RetrievalService | None = None) -> None:
        self.retrieval = retrieval or RetrievalService()

    async def personalize(
        self,
        db: AsyncSession,
        principal: Principal,
        *,
        context: str | None = None,
    ) -> dict[str, Any]:
        """Return the user's current personalization context."""
        now = _now()

        profile = (
            await db.execute(
                select(UserProfile).where(
                    UserProfile.user_id == principal.user_id,
                    UserProfile.tenant_id == principal.tenant_id,
                )
            )
        ).scalar_one_or_none()

        # Memories surfaced for the current context (or a general summary).
        memories, _ = await self.retrieval.retrieve(
            db,
            principal,
            RetrievalRequest(
                query_text=context or _DEFAULT_CONTEXT,
                k=10,
                token_budget=1200,
            ),
        )

        preferences = [m for m in memories if m.kind == "preference"]
        goals = [m for m in memories if m.kind == "goal"]
        commitments = [
            m
            for m in memories
            if m.kind == "commitment"
            and m.due_at is not None
            and m.due_at <= now + _COMMITMENT_DUE_WINDOW
        ]
        projects = [m for m in memories if m.kind == "project"]
        relationships = [m for m in memories if m.kind == "relationship"]

        return {
            "user_id": str(principal.user_id),
            "tenant_id": str(principal.tenant_id),
            "generated_at": now.isoformat(),
            "communication_style": profile.communication_style if profile else None,
            "timezone": profile.timezone if profile else "UTC",
            "locale": profile.locale if profile else "en",
            "preferences": [self._mem_summary(m) for m in preferences],
            "goals": [self._mem_summary(m) for m in goals],
            "commitments": [self._mem_summary(m) for m in commitments],
            "projects": [self._mem_summary(m) for m in projects],
            "relevant_people": [self._mem_summary(m) for m in relationships],
            "recent_memories": [self._mem_summary(m) for m in memories[:5]],
        }

    @staticmethod
    def _mem_summary(m: RetrievedMemory) -> dict[str, Any]:
        return {
            "id": str(m.id),
            "kind": m.kind,
            "title": m.title,
            "content": m.content,
            "score": m.score,
            "importance": m.importance,
            "confidence": m.confidence,
            "sources": m.sources,
            "occurred_at": m.occurred_at,
            "due_at": m.due_at.isoformat() if m.due_at else None,
        }
