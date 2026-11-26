"""Verification and anti-hallucination checks (Prompt 06)."""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from typing import Literal

from zara.agent.state import (
    AgentState,
    Citation,
    Plan,
    ToolResultState,
    VerificationResult,
)

_COMPLETED_CLAIM_RE = re.compile(
    r"\b(i (?:have )?(?:sent|created|scheduled|shared|deleted|updated|moved|posted|merged|"
    r"completed|executed)|successfully (?:sent|created|scheduled|shared|deleted|updated)|"
    r"(?:it|that) is done)\b",
    re.IGNORECASE,
)


@dataclass(slots=True)
class Verification:
    result: VerificationResult
    issues: list[str] = field(default_factory=list)


def verify_tools(plan: Plan | None, results: list[ToolResultState]) -> Verification:
    if plan is None:
        return Verification(
            result=VerificationResult(
                goal_satisfied="partial",
                issues=["no plan"],
                next_action="respond",
            )
        )
    issues: list[str] = []
    tool_steps = [step for step in plan.steps if step.tool is not None]
    succeeded = {result.tool for result in results if result.status == "succeeded"}
    pending = [result for result in results if result.status == "pending_approval"]
    failed = [result for result in results if result.status in {"failed", "timed_out", "denied"}]
    if pending:
        issues.append("one or more actions are waiting for approval and have not run")
    if failed:
        issues.extend(f"{result.tool} ended with status {result.status}" for result in failed)
    blocked = [step for step in plan.steps if step.status == "blocked"]
    if blocked:
        issues.extend(f"step {step.id} was blocked before execution" for step in blocked)
    missing = [
        step.tool for step in tool_steps if step.tool is not None and step.tool not in succeeded
    ]
    if missing:
        issues.append("tools without a verified success: " + ", ".join(sorted(set(missing))))

    next_action: Literal["continue", "replan", "respond", "fail"]
    satisfied: bool | str
    if pending:
        next_action = "respond"
        satisfied = "partial"
    elif failed:
        next_action = "fail"
        satisfied = "partial" if succeeded else False
    elif blocked or missing:
        next_action = "replan"
        satisfied = "partial" if succeeded else False
    elif tool_steps:
        next_action = "respond"
        satisfied = True
    else:
        next_action = "respond"
        satisfied = "partial"

    return Verification(
        result=VerificationResult(
            goal_satisfied=satisfied,
            issues=issues,
            groundedness=1.0 if not issues else 0.0,
            unsupported_claims=[],
            next_action=next_action,
        )
    )


def verify_response(
    answer: str,
    citations: list[Citation],
    *,
    state: AgentState,
    allowed_memory_ids: set[uuid.UUID],
    succeeded_execution_ids: set[uuid.UUID],
) -> list[str]:
    """Return unsupported claims. Never mutates the answer."""
    issues: list[str] = []
    for citation in citations:
        if citation.type == "memory":
            try:
                citation_id = uuid.UUID(citation.id)
            except ValueError:
                issues.append(f"invalid memory citation {citation.id!r}")
                continue
            if citation_id not in allowed_memory_ids:
                issues.append(f"citation {citation.id} was not in retrieved context")
        elif citation.type == "tool_execution":
            try:
                citation_id = uuid.UUID(citation.id)
            except ValueError:
                issues.append(f"invalid execution citation {citation.id!r}")
                continue
            if citation_id not in succeeded_execution_ids:
                issues.append(f"execution {citation.id} did not succeed")
    if _COMPLETED_CLAIM_RE.search(answer) and not succeeded_execution_ids:
        issues.append("response claims a completed action but no tool execution succeeded")
    return issues


def combine_verification(
    base: VerificationResult, unsupported: list[str], citations: list[Citation]
) -> VerificationResult:
    groundedness = 1.0 if not citations else max(0.0, 1.0 - len(unsupported) / len(citations))
    issues = list(base.issues)
    if unsupported:
        issues.extend(unsupported)
    satisfied = base.goal_satisfied
    if unsupported and satisfied is True:
        satisfied = "partial"
    return VerificationResult(
        goal_satisfied=satisfied,
        issues=issues,
        groundedness=round(groundedness, 4),
        unsupported_claims=unsupported,
        next_action="fail" if unsupported and not citations else base.next_action,
    )
