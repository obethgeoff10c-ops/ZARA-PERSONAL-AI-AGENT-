"""Memory extraction pipeline (03-MEMORY-ENGINE.md §2).

Turns raw text spans (conversation turns, ingested documents, events) into
candidate memory items, runs them through safety/trust/dedup gates, and writes
the accepted ones to ``memory.memory_items`` with ``memory.memory_sources``
provenance.

The extractor is pluggable (``Extractor`` protocol). A deterministic heuristic
extractor ships as the default so the pipeline is testable without a real LLM;
swap in a real LLM-backed extractor when a provider is configured. Extraction
never simulates assistant output and never writes a memory it cannot attribute
to a verbatim source.
"""

from __future__ import annotations

import hashlib
import re
import uuid
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol, runtime_checkable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from zara.auth.principal import Principal
from zara.models import MemoryItem, MemorySource
from zara.services.audit import record_event
from zara.services.memory.dedup import check_duplicate_and_contradiction
from zara.services.memory.embeddings import store_current_embedding
from zara.services.memory.entities import resolve_entities
from zara.services.memory.keys import derive_canonical_key

# --- extraction gates (03 §2.4) ---------------------------------------------

_MIN_CONFIDENCE_ACTIVE = 0.75
_MIN_CONFIDENCE_SUGGESTED = 0.50
_MIN_CONFIDENCE_DROP = 0.50
_IMPORTANCE_FLOOR = 0.20

# Phrases that look like instructions/injection addressed to the assistant.
_INJECTION_PATTERNS = [
    r"ignore (previous|all|your) (instructions|system)",
    r"you are now (a|an)",
    r"disregard",
    r"jailbreak",
    r"prompt ?[- ]?injection",
]
_INJECTION_RE = re.compile("|".join(_INJECTION_PATTERNS), re.IGNORECASE)

# Crude secret detector (the crypto milestone replaces with a proper scanner).
_SECRET_RE = re.compile(r"(?i)(api[_-]?key|secret|token|password)\s*[:=]\s*[A-Za-z0-9_-]{16,}")

# Sensitive-content classifier (health, government id, financial account).
_SENSITIVE_RE = re.compile(
    r"(ssn|social security|credit card|card number|password|api key|secret key|"
    r"passport|driver'?s? license|medical record|health condition|diagnosis)",
    re.IGNORECASE,
)


# Common capitalized words that are not entities in the heuristic extractor.
_ENTITY_STOPWORDS = {
    "I",
    "My",
    "The",
    "A",
    "An",
    "This",
    "That",
    "These",
    "Those",
    "We",
    "Our",
    "You",
    "Your",
    "It",
    "Its",
    "He",
    "She",
    "They",
    "Their",
    "In",
    "On",
    "At",
    "For",
    "And",
    "But",
    "Or",
    "When",
    "Where",
    "What",
    "Who",
    "Why",
    "How",
    "Is",
    "Are",
    "Was",
    "Were",
    "Monday",
    "Tuesday",
    "Wednesday",
    "Thursday",
    "Friday",
    "Saturday",
    "Sunday",
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
}

# Tokens removed when deriving a canonical key so a negated restatement maps to
# the same key as the original claim (contradiction detection).


def _now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class EntityCandidate:
    surface: str
    type: str
    role: str  # "subject" | "object" | "mentioned"


@dataclass(frozen=True)
class Temporal:
    occurred_at: datetime | None = None
    valid_from: datetime | None = None
    valid_to: datetime | None = None
    due_at: datetime | None = None
    expires_hint: str | None = None  # "ephemeral" | "short" | "medium" | "long" | "permanent"


@dataclass(frozen=True)
class Evidence:
    source_id: str
    quote: str
    span: tuple[int, int] = (0, 0)


@dataclass(frozen=True)
class ExtractionCandidate:
    """One candidate memory produced by the extractor (03 §2.3 schema)."""

    kind: str
    title: str
    content: str
    payload: dict[str, Any]
    entities: list[EntityCandidate] = field(default_factory=list)
    temporal: Temporal = field(default_factory=Temporal)
    confidence: float = 0.0
    importance: float = 0.0
    sensitivity: str = "normal"
    evidence: list[Evidence] = field(default_factory=list)
    canonical_key: str | None = None
    reasoning: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "title": self.title,
            "content": self.content,
            "payload": self.payload,
            "entities": [
                {"surface": e.surface, "type": e.type, "role": e.role} for e in self.entities
            ],
            "temporal": {
                "occurred_at": _iso(self.temporal.occurred_at),
                "valid_from": _iso(self.temporal.valid_from),
                "valid_to": _iso(self.temporal.valid_to),
                "due_at": _iso(self.temporal.due_at),
                "expires_hint": self.temporal.expires_hint,
            },
            "confidence": self.confidence,
            "importance": self.importance,
            "sensitivity": self.sensitivity,
            "evidence": [
                {"source_id": ev.source_id, "quote": ev.quote, "span": list(ev.span)}
                for ev in self.evidence
            ],
            "canonical_key": self.canonical_key,
            "reasoning": self.reasoning,
        }


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _apply_sensitivity(candidate: ExtractionCandidate) -> ExtractionCandidate:
    """Escalate sensitivity to ``restricted`` when content looks sensitive."""
    if candidate.sensitivity == "restricted":
        return candidate
    haystack = f"{candidate.title} {candidate.content}"
    if _SENSITIVE_RE.search(haystack):
        return replace(candidate, sensitivity="restricted")
    return candidate


@runtime_checkable
class Extractor(Protocol):
    """Extract memory candidates from a text span.

    Implementations must return only candidates supported by verbatim evidence
    in ``text``; they must never invent facts.
    """

    async def extract(
        self,
        *,
        text: str,
        source_id: str,
        trust_level: str,
        reference_time: datetime,
    ) -> list[ExtractionCandidate]: ...


class HeuristicExtractor:
    """Deterministic, dependency-free extractor for tests and local dev.

    Splits text into sentences and classifies each into one of the persisted
    memory kinds using explicit cue phrases (preference, goal, commitment,
    relationship, project, routine). Everything else is ``episodic``. Quotes are
    verbatim substrings of the source text so the provenance gate passes.
    """

    _PREFERENCE_RE = re.compile(r"\bI (?:prefer|like|love|hate|dislike|avoid)\b", re.IGNORECASE)
    _GOAL_RE = re.compile(
        r"\b(?:my goal|objective|planning|plan to|i want to|aiming to)\b", re.IGNORECASE
    )
    _COMMITMENT_RE = re.compile(
        r"\b(?:i will|i promise|i commit|due on|due by|deadline)\b", re.IGNORECASE
    )
    _RELATIONSHIP_RE = re.compile(
        r"\b(?:my (?:sister|brother|mother|father|friend|colleague|manager|"
        r"wife|husband|partner|son|daughter))\b",
        re.IGNORECASE,
    )
    _PROJECT_RE = re.compile(r"\b(?:project|initiative|launch|roadmap)\b", re.IGNORECASE)
    _ROUTINE_RE = re.compile(
        r"\b(?:every (?:day|week|monday|tuesday|wednesday|thursday|friday|"
        r"saturday|sunday)|routine|usually)\b",
        re.IGNORECASE,
    )
    _SEMANTIC_RE = re.compile(
        r"\b(?:works at|work at|is based in|are based in|lives in|live in|"
        r"founded|was founded|has \d+|have \d+|is a|is an|are a)\b",
        re.IGNORECASE,
    )
    _ENTITY_RE = re.compile(r"\b[A-Z][a-z]+(?:\s+[A-Z][a-z]+)*\b")

    _CLASSIFIERS: tuple[tuple[str, re.Pattern[str], float, float], ...] = (
        ("preference", _PREFERENCE_RE, 0.9, 0.6),
        ("commitment", _COMMITMENT_RE, 0.85, 0.7),
        ("goal", _GOAL_RE, 0.85, 0.7),
        ("relationship", _RELATIONSHIP_RE, 0.85, 0.6),
        ("project", _PROJECT_RE, 0.8, 0.55),
        ("procedural", _ROUTINE_RE, 0.8, 0.5),
        ("semantic", _SEMANTIC_RE, 0.8, 0.5),
    )

    async def extract(
        self,
        *,
        text: str,
        source_id: str,
        trust_level: str,
        reference_time: datetime,
    ) -> list[ExtractionCandidate]:
        candidates: list[ExtractionCandidate] = []
        sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", text) if s.strip()]
        for sentence in sentences:
            if len(sentence) < 6:
                continue
            entities = [
                EntityCandidate(surface=m.group(0), type="person", role="mentioned")
                for m in self._ENTITY_RE.finditer(sentence)
                if m.group(0) not in _ENTITY_STOPWORDS
            ]
            kind, confidence, importance = self._classify(sentence)
            candidates.append(
                ExtractionCandidate(
                    kind=kind,
                    title=f"{kind.capitalize()}: {sentence[:60]}",
                    content=sentence,
                    payload={"event_type": "mention"},
                    entities=entities,
                    temporal=Temporal(occurred_at=reference_time, expires_hint="long"),
                    confidence=confidence,
                    importance=importance,
                    sensitivity="normal",
                    evidence=[
                        Evidence(source_id=source_id, quote=sentence, span=(0, len(sentence)))
                    ],
                    canonical_key=derive_canonical_key(sentence),
                    reasoning="heuristic sentence split",
                )
            )
        return candidates

    @classmethod
    def _classify(cls, sentence: str) -> tuple[str, float, float]:
        for kind, pattern, confidence, importance in cls._CLASSIFIERS:
            if pattern.search(sentence):
                return kind, confidence, importance
        return "episodic", 0.8, 0.35


@dataclass(frozen=True)
class ExtractionResult:
    accepted: list[MemoryItem]
    suggested: list[MemoryItem]
    rejected: list[dict[str, Any]]


class ExtractionPipeline:
    """Run candidates through the extraction gates and write to the DB."""

    def __init__(self, extractor: Extractor | None = None) -> None:
        self.extractor = extractor or HeuristicExtractor()

    async def extract_and_store(
        self,
        db: AsyncSession,
        principal: Principal,
        *,
        text: str,
        source_id: str,
        source_type: str = "message",
        trust_level: str = "user",
        reference_time: datetime | None = None,
        message_id: uuid.UUID | None = None,
        run_id: uuid.UUID | None = None,
    ) -> ExtractionResult:
        """Extract candidates from ``text`` and persist accepted/suggested ones.

        Every written memory gets at least one ``memory_sources`` row quoting
        verbatim evidence from ``text`` (anti-hallucination).
        """
        reference_time = reference_time or _now()
        raw_candidates = await self.extractor.extract(
            text=text,
            source_id=source_id,
            trust_level=trust_level,
            reference_time=reference_time,
        )

        accepted: list[MemoryItem] = []
        suggested: list[MemoryItem] = []
        rejected: list[dict[str, Any]] = []

        for candidate in raw_candidates:
            gate = self._gate(candidate, trust_level, text)
            if gate is not None:
                rejected.append({"candidate": candidate.to_dict(), "reason": gate})
                continue

            candidate = _apply_sensitivity(candidate)
            content_hash = hashlib.sha256(candidate.content.encode("utf-8")).digest()
            dedup = await check_duplicate_and_contradiction(
                db,
                principal,
                kind=candidate.kind,
                content=candidate.content,
                content_hash=content_hash,
                canonical_key=candidate.canonical_key,
                entities=[
                    {"surface": e.surface, "type": e.type, "role": e.role}
                    for e in candidate.entities
                ],
                confidence=candidate.confidence,
                importance=candidate.importance,
                trust_level=trust_level,
                sensitivity=candidate.sensitivity,
                reference_time=reference_time,
            )

            if dedup.action == "drop":
                rejected.append({"candidate": candidate.to_dict(), "reason": "duplicate"})
                continue

            if dedup.action == "blocked":
                rejected.append({"candidate": candidate.to_dict(), "reason": "blocked_fact"})
                continue

            if dedup.action == "refines":
                # Update the existing item with a revision (new content).
                assert dedup.existing_id is not None
                existing = await _get_memory(db, principal, dedup.existing_id)
                if existing is not None:
                    await _refine_existing(db, principal, existing, candidate.content)
                rejected.append({"candidate": candidate.to_dict(), "reason": "refines_existing"})
                continue

            if dedup.action == "contradicts":
                existing = (
                    await _get_memory(db, principal, dedup.existing_id)
                    if dedup.existing_id is not None
                    else None
                )
                if existing is not None and (existing.user_pinned or existing.user_edited):
                    # Precedence: user-corrected/pinned memories are authoritative.
                    status = "suggested"
                elif trust_level == "user":
                    # User source wins: supersede existing, insert candidate as active.
                    if existing is not None:
                        existing.status = "superseded"
                        existing.valid_to = _now()
                    status = "active"
                else:
                    status = "suggested"
            elif dedup.action == "near_duplicate":
                # Still insert, but flagged; consolidation handles it later.
                status = "suggested" if candidate.confidence < _MIN_CONFIDENCE_ACTIVE else "active"
            else:
                # insert / unrelated
                if candidate.sensitivity == "restricted":
                    status = "suggested"
                elif candidate.confidence >= _MIN_CONFIDENCE_ACTIVE:
                    status = "active"
                else:
                    status = "suggested"

            # Sensitive/restricted content is never auto-activated; it waits for
            # explicit user confirmation (03 §2.4 Sensitivity).
            if candidate.sensitivity == "restricted":
                status = "suggested"

            memory = await self._write_memory(
                db,
                principal,
                candidate,
                status=status,
                source_type=source_type,
                source_id=source_id,
                message_id=message_id,
                run_id=run_id,
                content_hash=content_hash,
                reference_time=reference_time,
                trust_level=trust_level,
            )
            if status == "suggested":
                suggested.append(memory)
            else:
                accepted.append(memory)

        await db.flush()
        return ExtractionResult(accepted=accepted, suggested=suggested, rejected=rejected)

    # -- gates ----------------------------------------------------------------

    def _gate(
        self, candidate: ExtractionCandidate, trust_level: str, source_text: str
    ) -> str | None:
        # Trust gate: external_untrusted sources may not create
        # preference/procedural/goal (03 §2.4 Trust).
        if trust_level == "external_untrusted" and candidate.kind in {
            "preference",
            "procedural",
            "goal",
        }:
            return f"external_untrusted source cannot create {candidate.kind} memory"

        # Provenance: every evidence quote must be a verbatim substring of the
        # source text. This is the anti-hallucination gate: an extractor that
        # invents a supporting quote is rejected outright.
        if not candidate.evidence:
            return "no provenance evidence"
        for evidence in candidate.evidence:
            if not evidence.quote or evidence.quote not in source_text:
                return "evidence quote is not verbatim in source text"

        # Secrets detector.
        if _SECRET_RE.search(candidate.content) or _SECRET_RE.search(candidate.title):
            return "candidate contains a possible secret; rejected"

        # Injection heuristics.
        if _INJECTION_RE.search(candidate.content):
            return "candidate matches injection heuristic"

        # Confidence thresholds.
        if candidate.confidence < _MIN_CONFIDENCE_DROP:
            return "confidence below drop threshold"
        # Importance floor for episodic.
        if candidate.kind == "episodic" and candidate.importance < _IMPORTANCE_FLOOR:
            return "episodic importance below floor"

        return None

    # -- write ----------------------------------------------------------------

    @staticmethod
    async def _available_canonical_key(
        db: AsyncSession, principal: Principal, canonical_key: str | None
    ) -> str | None:
        """Drop the key when another live item already owns it.

        ``ix_memory_items_user_canonical_key`` is unique for active rows, so a
        candidate judged ``unrelated`` must not reuse an occupied key.
        """
        if canonical_key is None:
            return None
        existing = (
            await db.execute(
                select(MemoryItem.id).where(
                    MemoryItem.tenant_id == principal.tenant_id,
                    MemoryItem.user_id == principal.user_id,
                    MemoryItem.canonical_key == canonical_key,
                    MemoryItem.status == "active",
                )
            )
        ).first()
        return None if existing is not None else canonical_key

    async def _write_memory(
        self,
        db: AsyncSession,
        principal: Principal,
        candidate: ExtractionCandidate,
        *,
        status: str,
        source_type: str,
        source_id: str,
        message_id: uuid.UUID | None,
        run_id: uuid.UUID | None,
        content_hash: bytes,
        reference_time: datetime,
        trust_level: str,
    ) -> MemoryItem:
        expires_at = self._expires_at(candidate, reference_time)
        canonical_key = await self._available_canonical_key(db, principal, candidate.canonical_key)
        memory = MemoryItem(
            tenant_id=principal.tenant_id,
            user_id=principal.user_id,
            kind=candidate.kind,
            title=candidate.title,
            content=candidate.content,
            payload=candidate.payload,
            importance=candidate.importance,
            confidence=candidate.confidence,
            sensitivity=candidate.sensitivity,
            trust_level=trust_level,
            status=status,
            valid_from=candidate.temporal.valid_from or reference_time,
            valid_to=candidate.temporal.valid_to,
            occurred_at=candidate.temporal.occurred_at,
            expires_at=expires_at,
            due_at=candidate.temporal.due_at,
            canonical_key=canonical_key,
            content_hash=content_hash,
            created_by="extractor",
            extraction_run_id=run_id,
        )
        db.add(memory)
        await db.flush()
        for evidence in candidate.evidence:
            db.add(
                MemorySource(
                    tenant_id=principal.tenant_id,
                    memory_id=memory.id,
                    source_type=source_type,
                    message_id=message_id,
                    run_id=run_id,
                    span_start=evidence.span[0],
                    span_end=evidence.span[1],
                    quote=evidence.quote[:300] if evidence.quote else None,
                    observed_at=reference_time,
                )
            )
        await store_current_embedding(
            db,
            tenant_id=principal.tenant_id,
            user_id=principal.user_id,
            memory_id=memory.id,
            text=f"{memory.title or ''} {memory.content}".strip(),
        )
        resolved = await resolve_entities(
            db,
            principal,
            memory=memory,
            candidates=candidate.entities,
            message_id=message_id,
            reference_time=reference_time,
        )
        if resolved:
            memory.payload = {
                **memory.payload,
                "entities": [str(entity.id) for entity in resolved],
            }
            await db.flush()
        await record_event(
            db,
            principal,
            action="memory.created",
            resource_type="memory_item",
            resource_id=str(memory.id),
            metadata={"kind": memory.kind, "status": status, "created_by": "extractor"},
        )
        return memory

    @staticmethod
    def _expires_at(candidate: ExtractionCandidate, reference_time: datetime) -> datetime | None:
        hint = (candidate.temporal.expires_hint or "permanent").lower()
        offsets = {
            "ephemeral": timedelta(hours=24),
            "short": timedelta(days=14),
            "medium": timedelta(days=90),
            "long": timedelta(days=365),
        }
        if hint == "permanent" or hint not in offsets:
            return None
        base = candidate.temporal.valid_to or reference_time
        return base + offsets[hint]


def trust_level_for(kind: str, source_trust: str) -> str:
    """Apply the trust gate: external_untrusted never becomes 'user'/'zara' for
    sensitive kinds; it stays 'external_untrusted' unless confirmed by user."""
    if source_trust == "external_untrusted" and kind in {"preference", "procedural", "goal"}:
        return "external_untrusted"
    return source_trust


async def _get_memory(
    db: AsyncSession, principal: Principal, memory_id: uuid.UUID
) -> MemoryItem | None:
    return (
        await db.execute(
            select(MemoryItem).where(
                MemoryItem.id == memory_id,
                MemoryItem.tenant_id == principal.tenant_id,
                MemoryItem.user_id == principal.user_id,
            )
        )
    ).scalar_one_or_none()


async def _refine_existing(
    db: AsyncSession, principal: Principal, existing: MemoryItem, new_content: str
) -> None:
    """Fold new content into an existing item as a revision (refines verdict)."""
    from zara.services.memory import snapshot_revision

    existing.content = new_content
    existing.content_hash = hashlib.sha256(new_content.encode("utf-8")).digest()
    existing.confidence = max(existing.confidence, 0.9)
    existing.user_edited = False
    await snapshot_revision(db, existing)
    await db.flush()
    await store_current_embedding(
        db,
        tenant_id=principal.tenant_id,
        user_id=principal.user_id,
        memory_id=existing.id,
        text=f"{existing.title or ''} {existing.content}".strip(),
    )
    await record_event(
        db,
        principal,
        action="memory.updated",
        resource_type="memory_item",
        resource_id=str(existing.id),
        metadata={"kind": existing.kind, "reason": "refines"},
    )
