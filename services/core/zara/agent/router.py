"""Context Router: deterministic signals + optional LLM merge (04 §5)."""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, Field
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from zara.agent.state import (
    Domain,
    DomainScore,
    EntityRef,
    Intent,
    RetrievalPlan,
    RiskPrescreen,
    RoutingDecision,
    SessionContext,
    TrustLabel,
    UserInput,
)
from zara.auth.principal import Principal
from zara.llm.base import LLMError, LLMMessage
from zara.llm.service import ModelService
from zara.models import Entity, EntityRelationship

_HIGH_PRECISION_INTENTS = {
    "set_reminder",
    "check_calendar",
    "check_pr_status",
    "search_memory",
    "draft_email",
    "schedule_meeting",
}

_INTENT_RULES: tuple[tuple[str, re.Pattern[str], bool, bool], ...] = (
    (
        "chitchat",
        re.compile(r"^\s*(thanks|thank you|hi|hello|hey|ok(?:ay)?)\b", re.I),
        False,
        False,
    ),
    (
        "set_reminder",
        re.compile(
            r"\b(remind me to|reminder|don't let me forget|remind me (?:about|at|on))\b",
            re.I,
        ),
        True,
        True,
    ),
    (
        "draft_email",
        re.compile(r"\b(draft|write|compose|send)\b.{0,40}\b(email|mail|reply)\b", re.I),
        True,
        True,
    ),
    (
        "schedule_meeting",
        re.compile(r"\b(schedule|book|reschedule|move)\b.{0,40}\b(meeting|call|event)\b", re.I),
        True,
        True,
    ),
    (
        "prepare_for_meeting",
        re.compile(r"\b(prep(?:are)? me|meeting prep|prepare for)\b", re.I),
        True,
        False,
    ),
    (
        "check_pr_status",
        re.compile(r"\b(pull request|pr|github|ci|build|merge|release)\b", re.I),
        True,
        False,
    ),
    (
        "pipeline_review",
        re.compile(r"\b(pipeline|deal|prospect|lead|opportunity|quote|proposal)\b", re.I),
        True,
        False,
    ),
    (
        "summarize_thread",
        re.compile(r"\b(summarize|summary|recap|thread|email chain)\b", re.I),
        True,
        False,
    ),
    (
        "check_calendar",
        re.compile(r"\b(calendar|agenda|what'?s on|week looking|free time)\b", re.I),
        False,
        False,
    ),
    (
        "daily_brief",
        re.compile(r"\b(what should i do|today|priorities|daily brief|plan my day)\b", re.I),
        True,
        False,
    ),
    (
        "search_memory",
        re.compile(r"\b(remember|recall|last time|preferred|approach i|what do you know)\b", re.I),
        False,
        False,
    ),
    (
        "track_goal",
        re.compile(r"\b(goal|habit|routine|progress)\b", re.I),
        True,
        False,
    ),
    (
        "draft_document",
        re.compile(r"\b(draft|write|create)\b.{0,30}\b(doc|document|brief|memo)\b", re.I),
        True,
        True,
    ),
)

_DOMAIN_SIGNALS: dict[Domain, tuple[tuple[re.Pattern[str], float], ...]] = {
    Domain.PERSONAL: (
        (
            re.compile(
                r"\b(remind|reminder|personal|family|mom|dad|birthday|habit|routine)\b",
                re.I,
            ),
            0.7,
        ),
        (re.compile(r"\b(preferred|preference|last time|approach i|how i like)\b", re.I), 0.6),
        (re.compile(r"\b(prepare me|prep me)\b", re.I), 0.45),
    ),
    Domain.EXECUTIVE: (
        (
            re.compile(
                r"\b(meeting|calendar|agenda|schedule|deadline|priority|priorities)\b",
                re.I,
            ),
            0.65,
        ),
        (re.compile(r"\b(decision|commitment|follow up|brief|week looking)\b", re.I), 0.55),
        (re.compile(r"\b(what should i do|today)\b", re.I), 0.55),
        (re.compile(r"\b(prep(?:are)? me|prepare for)\b", re.I), 0.6),
    ),
    Domain.SALES: (
        (re.compile(r"\b(prospect|deal|pipeline|lead|opportunity|quote|proposal)\b", re.I), 0.75),
        (re.compile(r"\b(client|customer|account|renewal|follow up)\b", re.I), 0.5),
        (re.compile(r"\b(crm|contact|company)\b", re.I), 0.45),
    ),
    Domain.DEVELOPER: (
        (re.compile(r"\b(github|pull request|pr|repo|repository|commit|branch)\b", re.I), 0.75),
        (re.compile(r"\b(ci|build|deploy|release|bug|issue|merge)\b", re.I), 0.6),
        (re.compile(r"\b(code|test|failing)\b", re.I), 0.4),
    ),
}

_KIND_BY_INTENT: dict[str, list[str]] = {
    "search_memory": ["preference", "procedural", "semantic", "relationship"],
    "prepare_for_meeting": ["relationship", "commitment", "episodic", "project"],
    "daily_brief": ["commitment", "goal", "project", "episodic"],
    "check_calendar": ["commitment", "episodic", "goal"],
    "check_pr_status": ["project", "commitment", "episodic"],
    "pipeline_review": ["relationship", "commitment", "project"],
    "track_goal": ["goal", "commitment", "procedural"],
    "summarize_thread": ["episodic", "relationship", "commitment"],
}

_EMAIL_RE = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")
_REPO_RE = re.compile(r"\b([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)\b")
_ISSUE_RE = re.compile(r"(?<!\w)#(\d{1,6})\b")
_TIME_RE = re.compile(
    r"\b(today|tomorrow|tonight|this week|next week|monday|tuesday|wednesday|thursday|"
    r"friday|saturday|sunday|\d{1,2}(?::\d{2})?\s?(?:am|pm))\b",
    re.I,
)


@dataclass(frozen=True)
class _Signal:
    score: float
    reasons: tuple[str, ...]


class _DomainDraft(BaseModel):
    domain: str
    score: float = Field(ge=0.0, le=1.0)
    reasons: list[str] = Field(default_factory=list)


class _IntentDraft(BaseModel):
    label: str
    confidence: float = Field(ge=0.0, le=1.0)
    needs_plan: bool = False
    write_likely: bool = False


class RoutingDraft(BaseModel):
    """Structured model output merged with Stage-A signals."""

    domains: list[_DomainDraft] = Field(default_factory=list)
    intent: _IntentDraft
    required_integrations: list[str] = Field(default_factory=list)
    entity_hints: list[str] = Field(default_factory=list)


class ContextRouter:
    def __init__(self, model: ModelService | None = None) -> None:
        self.model = model

    async def route(
        self,
        db: AsyncSession,
        principal: Principal,
        user_input: UserInput,
        session: SessionContext,
        *,
        recent_domains: list[Domain] | None = None,
    ) -> RoutingDecision:
        text = user_input.text or ""
        signals, intent, matched_high_precision = self._stage_a(
            text, user_input, recent_domains or []
        )
        entities = await self._resolve_entities(db, principal, text)
        signals = await self._graph_expansion(db, principal, entities, signals)

        fallback: str | None = None
        draft: RoutingDraft | None = None
        should_classify = (
            self.model is not None
            and self.model.available
            and not self._decisive(signals, intent, matched_high_precision)
        )
        if should_classify:
            try:
                draft = await self._classify_with_model(text, session, signals, entities)
            except LLMError:
                fallback = "deterministic_only"

        if draft is not None:
            signals = self._merge(draft, signals)
            intent = Intent(
                label=draft.intent.label,
                confidence=draft.intent.confidence,
                needs_plan=draft.intent.needs_plan,
                write_likely=draft.intent.write_likely,
            )
        elif fallback is None and (
            not signals or max(scores.score for scores in signals.values()) < 0.35
        ):
            fallback = "personal_default"

        active = self._active_domains(signals)
        if not active:
            active = [Domain.PERSONAL]
            signals.setdefault(
                Domain.PERSONAL,
                _Signal(0.45, ("fallback: no strong domain signal",)),
            )
        primary = max(active, key=lambda domain: signals[domain].score)
        domain_scores = [
            DomainScore(
                domain=domain,
                score=round(signals[domain].score if domain in signals else 0.0, 4),
                reasons=list(signals[domain].reasons) if domain in signals else [],
                primary=domain == primary,
            )
            for domain in Domain
        ]
        confidence = self._confidence(signals, active, entities, intent)
        required = sorted(
            set(draft.required_integrations if draft else [])
            | set(self._required_integrations(active))
        )
        retrieval = RetrievalPlan(
            kinds=_KIND_BY_INTENT.get(intent.label, []),
            entity_ids=[
                entity.resolved_entity_id
                for entity in entities
                if entity.resolved_entity_id is not None
            ],
            k=14 if intent.needs_plan else 10,
            token_budget=2200 if len(active) > 1 else 1600,
        )
        return RoutingDecision(
            domains=domain_scores,
            active_domains=active,
            intent=intent,
            entities=entities,
            time_scope=self._time_scope(text, session.now),
            required_integrations=required,
            risk_prescreen=RiskPrescreen(max_tier=0),
            retrieval_plan=retrieval,
            fallback_used=fallback,
            confidence=confidence,
        )

    def _stage_a(
        self,
        text: str,
        user_input: UserInput,
        recent_domains: list[Domain],
    ) -> tuple[dict[Domain, _Signal], Intent, bool]:
        scores: dict[Domain, float] = defaultdict(float)
        reasons: dict[Domain, list[str]] = defaultdict(list)
        for domain, rules in _DOMAIN_SIGNALS.items():
            for pattern, weight in rules:
                matches = pattern.findall(text)
                if matches:
                    scores[domain] += weight
                    reasons[domain].append(f"matched: {matches[0]}")
        if user_input.trust == TrustLabel.EXTERNAL:
            scores[Domain.EXECUTIVE] += 0.2
            reasons[Domain.EXECUTIVE].append("external event source")
        for domain in recent_domains[-3:]:
            scores[domain] += 0.1
            reasons[domain].append("recent conversation activity")

        label, confidence, needs_plan, write_likely, high_precision = self._intent(text)
        if label == "check_pr_status":
            scores[Domain.DEVELOPER] += 0.2
        if label == "pipeline_review":
            scores[Domain.SALES] += 0.2
        if label in {"daily_brief", "check_calendar"}:
            scores[Domain.EXECUTIVE] += 0.2
            scores[Domain.PERSONAL] += 0.35

        normalized = {
            domain: _Signal(min(1.0, round(score, 4)), tuple(reasons[domain]))
            for domain, score in scores.items()
            if score > 0
        }
        return (
            normalized,
            Intent(
                label=label,
                confidence=confidence,
                needs_plan=needs_plan,
                write_likely=write_likely,
            ),
            high_precision,
        )

    def _intent(self, text: str) -> tuple[str, float, bool, bool, bool]:
        for label, pattern, needs_plan, write_likely in _INTENT_RULES:
            if pattern.search(text):
                confidence = 0.88 if label in _HIGH_PRECISION_INTENTS else 0.74
                return label, confidence, needs_plan, write_likely, label in _HIGH_PRECISION_INTENTS
        if "?" in text:
            return "answer_question", 0.55, False, False, False
        return "answer_question", 0.5, False, False, False

    def _decisive(
        self,
        signals: dict[Domain, _Signal],
        intent: Intent,
        matched_high_precision: bool,
    ) -> bool:
        if not signals:
            return False
        ranked = sorted((signal.score for signal in signals.values()), reverse=True)
        top = ranked[0]
        second = ranked[1] if len(ranked) > 1 else 0.0
        return top >= 0.85 and second <= 0.3 and matched_high_precision and intent.confidence >= 0.8

    async def _classify_with_model(
        self,
        text: str,
        session: SessionContext,
        signals: dict[Domain, _Signal],
        entities: list[EntityRef],
    ) -> RoutingDraft:
        assert self.model is not None
        signal_table = {
            domain.value: {
                "score": signal.score,
                "reasons": list(signal.reasons),
            }
            for domain, signal in signals.items()
        }
        system = (
            "You are ZARA's context router. Classify one request into one or more of "
            "PERSONAL, EXECUTIVE, SALES, DEVELOPER. Domains are lenses, not exclusive "
            "agents. Use the supplied deterministic signals and entity hints; do not invent "
            "entities. Return the structured routing object only."
        )
        user = (
            f"Request: {text}\n"
            f"Recent turns: {[m.content[:200] for m in session.recent_messages[-3:]]}\n"
            f"Stage-A signals: {signal_table}\n"
            f"Resolved entities: {[e.model_dump(mode='json') for e in entities]}\n"
            f"Timezone: {session.timezone}"
        )
        return await self.model.complete_json(
            system=system,
            messages=[LLMMessage(role="user", content=user)],
            model=RoutingDraft,
            max_tokens=700,
        )

    def _merge(
        self, draft: RoutingDraft, deterministic: dict[Domain, _Signal]
    ) -> dict[Domain, _Signal]:
        merged: dict[Domain, _Signal] = {}
        model_scores: dict[Domain, _DomainDraft] = {}
        for item in draft.domains:
            try:
                model_scores[Domain(item.domain.upper())] = item
            except ValueError:
                continue
        for domain in Domain:
            det = deterministic.get(domain)
            model = model_scores.get(domain)
            det_score = det.score if det else 0.0
            model_score = model.score if model else 0.0
            if model is not None and det is not None:
                score = 0.4 * det_score + 0.6 * model_score
            elif model is not None:
                score = model_score
            else:
                score = det_score
            reasons = list(det.reasons if det else ())
            if model:
                reasons.extend(model.reasons)
            if score > 0:
                merged[domain] = _Signal(round(min(1.0, score), 4), tuple(dict.fromkeys(reasons)))
        return merged

    def _active_domains(self, signals: dict[Domain, _Signal]) -> list[Domain]:
        ranked = sorted(signals.items(), key=lambda pair: pair[1].score, reverse=True)
        active = [domain for domain, signal in ranked if signal.score >= 0.35][:3]
        if not active and ranked:
            active = [ranked[0][0]]
        return active

    def _confidence(
        self,
        signals: dict[Domain, _Signal],
        active: list[Domain],
        entities: list[EntityRef],
        intent: Intent,
    ) -> float:
        ranked = sorted((signal.score for signal in signals.values()), reverse=True)
        top = ranked[0] if ranked else 0.0
        second = ranked[1] if len(ranked) > 1 else 0.0
        margin = max(0.0, top - second) / (top or 1.0)
        entity_conf = (
            sum(entity.confidence for entity in entities) / len(entities) if entities else 0.6
        )
        return round(min(1.0, 0.5 * intent.confidence + 0.3 * margin + 0.2 * entity_conf), 4)

    @staticmethod
    def _required_integrations(active: list[Domain]) -> list[str]:
        required: set[str] = set()
        if Domain.DEVELOPER in active:
            required.add("github")
        if Domain.EXECUTIVE in active:
            required.update({"gcal", "gmail"})
        if Domain.SALES in active:
            required.add("gmail")
        return sorted(required)

    def _time_scope(self, text: str, now: datetime) -> Any | None:
        match = _TIME_RE.search(text)
        if not match:
            return None
        period = match.group(1).lower()
        start = now.astimezone(UTC)
        if period == "tomorrow":
            start = start.replace(hour=0, minute=0, second=0, microsecond=0)
            start = start.fromtimestamp(start.timestamp() + 86400, UTC)
        elif period in {"today", "tonight"}:
            start = start.replace(hour=0, minute=0, second=0, microsecond=0)
        elif period in {"this week", "next week"}:
            start = start.replace(hour=0, minute=0, second=0, microsecond=0)
            if period == "next week":
                start = start.fromtimestamp(start.timestamp() + 7 * 86400, UTC)
        else:
            return None
        from zara.agent.state import TimeScope

        return TimeScope(start=start, end=start.fromtimestamp(start.timestamp() + 86400, UTC))

    async def _resolve_entities(
        self, db: AsyncSession, principal: Principal, text: str
    ) -> list[EntityRef]:
        found: dict[str, EntityRef] = {}
        surfaces: list[tuple[str, str, float]] = []
        for match in _EMAIL_RE.findall(text):
            surfaces.append((match, "person", 0.95))
        for match in _REPO_RE.findall(text):
            if "/" in match and not match.startswith(("http", "www")):
                surfaces.append((match, "repository", 0.8))
        for match in _ISSUE_RE.findall(text):
            surfaces.append((f"#{match}", "issue", 0.7))
        for surface, fallback_type, confidence in surfaces:
            found[surface] = EntityRef(
                surface=surface,
                type=fallback_type,
                resolution="deterministic",
                confidence=confidence,
            )
        # Resolve known names/aliases only when they appear verbatim; never guess.
        tokens = {
            token.strip(".,:;!?()[]\"'")
            for token in re.split(r"\s+", text)
            if len(token.strip(".,:;!?()[]\"'")) >= 3
        }
        if tokens:
            rows = (
                await db.execute(
                    select(Entity)
                    .where(
                        Entity.tenant_id == principal.tenant_id,
                        Entity.user_id == principal.user_id,
                        Entity.status == "active",
                        or_(
                            Entity.canonical_name.in_(tokens),
                            Entity.aliases.overlap(list(tokens)),
                        ),
                    )
                    .limit(10)
                )
            ).scalars()
            for entity in rows:
                found[entity.canonical_name] = EntityRef(
                    surface=entity.canonical_name,
                    type=entity.type,
                    resolved_entity_id=entity.id,
                    resolution="deterministic",
                    confidence=0.95,
                )
        return list(found.values())

    async def _graph_expansion(
        self,
        db: AsyncSession,
        principal: Principal,
        entities: list[EntityRef],
        signals: dict[Domain, _Signal],
    ) -> dict[Domain, _Signal]:
        entity_ids = [entity.resolved_entity_id for entity in entities if entity.resolved_entity_id]
        if not entity_ids:
            return signals
        rows = (
            await db.execute(
                select(Entity.type)
                .join(
                    EntityRelationship,
                    or_(
                        EntityRelationship.src_entity_id == Entity.id,
                        EntityRelationship.dst_entity_id == Entity.id,
                    ),
                )
                .where(
                    EntityRelationship.tenant_id == principal.tenant_id,
                    EntityRelationship.user_id == principal.user_id,
                    or_(
                        EntityRelationship.src_entity_id.in_(entity_ids),
                        EntityRelationship.dst_entity_id.in_(entity_ids),
                    ),
                )
                .limit(20)
            )
        ).scalars()
        domain_by_type = {
            "repository": Domain.DEVELOPER,
            "pull_request": Domain.DEVELOPER,
            "issue": Domain.DEVELOPER,
            "release": Domain.DEVELOPER,
            "company": Domain.SALES,
            "meeting": Domain.EXECUTIVE,
            "deadline": Domain.EXECUTIVE,
            "goal": Domain.PERSONAL,
        }
        expanded = dict(signals)
        for entity_type in rows:
            domain = domain_by_type.get(entity_type)
            if domain is None:
                continue
            prior = expanded.get(domain)
            base = prior.score if prior else 0.0
            reasons = list(prior.reasons if prior else ())
            if base < 0.6:
                reasons.append("graph_expansion")
                expanded[domain] = _Signal(round(min(1.0, base + 0.2), 4), tuple(reasons))
        return expanded
