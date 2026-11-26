"""Typed agent state shared by every graph node (04-AGENT-STATE-ROUTER.md §2)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field

STATE_SCHEMA_VERSION = 1


class TrustLabel(StrEnum):
    SYSTEM = "system"
    USER = "user"
    ZARA = "zara"
    EXTERNAL = "external_untrusted"


class Domain(StrEnum):
    PERSONAL = "PERSONAL"
    EXECUTIVE = "EXECUTIVE"
    SALES = "SALES"
    DEVELOPER = "DEVELOPER"


class RunStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    AWAITING_APPROVAL = "awaiting_approval"
    PAUSED = "paused"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


class AttachmentRef(BaseModel):
    id: uuid.UUID | None = None
    name: str | None = None
    mime_type: str | None = None


class UserInput(BaseModel):
    kind: Literal["user_message", "event", "schedule", "proactive_goal", "resume"]
    text: str | None = None
    message_id: uuid.UUID | None = None
    event_id: uuid.UUID | None = None
    attachments: list[AttachmentRef] = Field(default_factory=list)
    trust: TrustLabel
    received_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class RunBudget(BaseModel):
    max_steps: int = 12
    max_tokens: int = 60_000
    max_cost_usd: float = 1.0
    deadline: datetime


class MessageRef(BaseModel):
    role: str
    content: str
    created_at: datetime | None = None


class IntegrationStatus(BaseModel):
    status: str
    granted_scopes: list[str] = Field(default_factory=list)


class SessionContext(BaseModel):
    tenant_id: uuid.UUID
    user_id: uuid.UUID
    run_id: uuid.UUID
    conversation_id: uuid.UUID | None = None
    trace_id: str
    timezone: str = "UTC"
    locale: str = "en"
    now: datetime = Field(default_factory=lambda: datetime.now(UTC))
    working_hours: dict[str, Any] = Field(default_factory=dict)
    quiet_hours: dict[str, Any] = Field(default_factory=dict)
    autonomy: dict[str, str] = Field(default_factory=dict)
    connected_integrations: dict[str, IntegrationStatus] = Field(default_factory=dict)
    kill_switches: set[str] = Field(default_factory=set)
    budget: RunBudget
    recent_messages: list[MessageRef] = Field(default_factory=list)
    profile_summary: str = ""


class DomainScore(BaseModel):
    domain: Domain
    score: float = Field(ge=0.0, le=1.0)
    reasons: list[str] = Field(default_factory=list)
    primary: bool = False


class Intent(BaseModel):
    label: str
    confidence: float = Field(ge=0.0, le=1.0)
    needs_plan: bool = False
    write_likely: bool = False


class EntityRef(BaseModel):
    surface: str
    type: str
    resolved_entity_id: uuid.UUID | None = None
    resolution: Literal["deterministic", "llm", "none"] = "none"
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)


class TimeScope(BaseModel):
    start: datetime | None = None
    end: datetime | None = None


class RetrievalPlan(BaseModel):
    kinds: list[str] = Field(default_factory=list)
    entity_ids: list[uuid.UUID] = Field(default_factory=list)
    k: int = Field(default=12, ge=1, le=50)
    token_budget: int = Field(default=1800, ge=200, le=20_000)


class RiskPrescreen(BaseModel):
    max_tier: int = Field(default=0, ge=0, le=4)
    flags: list[str] = Field(default_factory=list)


class RoutingDecision(BaseModel):
    domains: list[DomainScore]
    active_domains: list[Domain]
    intent: Intent
    entities: list[EntityRef] = Field(default_factory=list)
    time_scope: TimeScope | None = None
    required_integrations: list[str] = Field(default_factory=list)
    risk_prescreen: RiskPrescreen = Field(default_factory=RiskPrescreen)
    retrieval_plan: RetrievalPlan = Field(default_factory=RetrievalPlan)
    fallback_used: str | None = None
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)

    def lower_domains(self) -> list[str]:
        return [domain.value.lower() for domain in self.active_domains]


class MemoryCard(BaseModel):
    id: uuid.UUID
    kind: str
    title: str | None
    content: str
    score: float
    confidence: float
    sensitivity: str
    sources: list[dict[str, Any]] = Field(default_factory=list)
    due_at: datetime | None = None


class EntityCard(BaseModel):
    id: uuid.UUID
    type: str
    name: str
    aliases: list[str] = Field(default_factory=list)
    attributes: dict[str, Any] = Field(default_factory=dict)


class RetrievedContext(BaseModel):
    memories: list[MemoryCard] = Field(default_factory=list)
    entities: list[EntityCard] = Field(default_factory=list)
    documents: list[dict[str, Any]] = Field(default_factory=list)
    recent_events: list[dict[str, Any]] = Field(default_factory=list)
    retrieval_log_id: str | None = None
    token_usage: dict[str, int] = Field(default_factory=dict)


class PlanStep(BaseModel):
    id: str
    description: str
    tool: str | None = None
    args: dict[str, Any] = Field(default_factory=dict)
    depends_on: list[str] = Field(default_factory=list)
    risk_tier: int = Field(default=0, ge=0, le=4)
    status: Literal["pending", "running", "done", "failed", "skipped", "blocked"] = "pending"
    result_ref: str | None = None
    approval_id: uuid.UUID | None = None
    parallel_group: int = 0
    requires_approval: bool = False


class Plan(BaseModel):
    version: int = 1
    goal: str
    steps: list[PlanStep] = Field(default_factory=list)
    rationale: str = ""
    parallel_groups: list[list[str]] = Field(default_factory=list)

    def step(self, step_id: str) -> PlanStep | None:
        return next((step for step in self.steps if step.id == step_id), None)


class ToolRequest(BaseModel):
    request_id: str
    tool: str
    args: dict[str, Any] = Field(default_factory=dict)
    derived_from: list[TrustLabel] = Field(default_factory=list)
    idempotency_key: str
    plan_step_id: str | None = None


class ToolErrorState(BaseModel):
    kind: str
    message: str


class ToolResultState(BaseModel):
    request_id: str
    execution_id: uuid.UUID
    tool: str
    status: Literal["succeeded", "failed", "denied", "timed_out", "pending_approval"]
    output: dict[str, Any] | None = None
    output_text_wrapped: str | None = None
    error: ToolErrorState | None = None
    approval_id: uuid.UUID | None = None
    latency_ms: int = 0
    trust: TrustLabel = TrustLabel.EXTERNAL


class VerificationResult(BaseModel):
    goal_satisfied: bool | Literal["partial"] = False
    issues: list[str] = Field(default_factory=list)
    groundedness: float = Field(default=0.0, ge=0.0, le=1.0)
    unsupported_claims: list[str] = Field(default_factory=list)
    next_action: Literal["continue", "replan", "respond", "fail"] = "respond"


class ApprovalState(BaseModel):
    pending: list[uuid.UUID] = Field(default_factory=list)
    decided: dict[str, str] = Field(default_factory=dict)
    blocked_since: datetime | None = None


class Citation(BaseModel):
    type: Literal["memory", "document", "entity", "tool_execution"]
    id: str
    label: str


class FinalOutput(BaseModel):
    text: str
    citations: list[Citation] = Field(default_factory=list)
    execution_summary: list[str] = Field(default_factory=list)
    proposed_actions: list[str] = Field(default_factory=list)
    usage: dict[str, Any] = Field(default_factory=dict)


class ErrorRecord(BaseModel):
    node: str
    kind: Literal[
        "llm",
        "tool",
        "policy",
        "budget",
        "validation",
        "timeout",
        "integration",
        "internal",
    ]
    message: str
    retryable: bool = False
    at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    step_id: str | None = None


class RunUsage(BaseModel):
    steps: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float = 0.0
    model_calls: int = 0
    tool_calls: int = 0

    def add_model(self, snapshot: dict[str, Any]) -> None:
        self.tokens_in = int(snapshot.get("tokens_in", 0))
        self.tokens_out = int(snapshot.get("tokens_out", 0))
        self.cost_usd = float(snapshot.get("cost_usd", 0.0))
        self.model_calls = int(snapshot.get("model_calls", 0))

    def as_dict(self) -> dict[str, Any]:
        return {
            "steps": self.steps,
            "tokens_in": self.tokens_in,
            "tokens_out": self.tokens_out,
            "cost_usd": round(self.cost_usd, 6),
            "model_calls": self.model_calls,
            "tool_calls": self.tool_calls,
        }


class AgentState(BaseModel):
    schema_version: int = STATE_SCHEMA_VERSION
    input: UserInput
    session: SessionContext
    routing: RoutingDecision | None = None
    retrieved: RetrievedContext | None = None
    goal: str | None = None
    plan: Plan | None = None
    scratch: list[dict[str, Any]] = Field(default_factory=list)
    tool_requests: list[ToolRequest] = Field(default_factory=list)
    tool_results: list[ToolResultState] = Field(default_factory=list)
    verification: VerificationResult | None = None
    approvals: ApprovalState = Field(default_factory=ApprovalState)
    taint: Literal["clean", "tainted"] = "clean"
    errors: list[ErrorRecord] = Field(default_factory=list)
    step_count: int = 0
    usage: RunUsage = Field(default_factory=RunUsage)
    final_output: FinalOutput | None = None
    status: RunStatus = RunStatus.QUEUED

    def compact_snapshot(self) -> dict[str, Any]:
        """Persist resumable pointers, never raw retrieved/untrusted bodies."""
        return {
            "schema_version": self.schema_version,
            "input": {
                "kind": self.input.kind,
                "text": self.input.text,
                "message_id": str(self.input.message_id) if self.input.message_id else None,
                "trust": self.input.trust.value,
                "received_at": self.input.received_at.isoformat(),
            },
            "session": {
                "run_id": str(self.session.run_id),
                "conversation_id": (
                    str(self.session.conversation_id) if self.session.conversation_id else None
                ),
                "trace_id": self.session.trace_id,
                "timezone": self.session.timezone,
                "now": self.session.now.isoformat(),
            },
            "routing": self.routing.model_dump(mode="json") if self.routing else None,
            "goal": self.goal,
            "plan": self.plan.model_dump(mode="json") if self.plan else None,
            "tool_results": [
                {
                    "request_id": result.request_id,
                    "execution_id": str(result.execution_id),
                    "tool": result.tool,
                    "status": result.status,
                    "approval_id": str(result.approval_id) if result.approval_id else None,
                }
                for result in self.tool_results
            ],
            "verification": (
                self.verification.model_dump(mode="json") if self.verification else None
            ),
            "approvals": self.approvals.model_dump(mode="json"),
            "taint": self.taint,
            "errors": [error.model_dump(mode="json") for error in self.errors],
            "step_count": self.step_count,
            "usage": self.usage.as_dict(),
            "status": self.status.value,
        }
