"""Grounded response composition with hallucination checks."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field

from zara.agent.prompts import build_prompt, composer_system_prompt
from zara.agent.state import (
    AgentState,
    Citation,
    FinalOutput,
    ToolResultState,
    VerificationResult,
)
from zara.llm.base import LLMError, LLMMessage
from zara.llm.service import ModelService
from zara.tools.registry import ToolRegistry


class ResponseDraft(BaseModel):
    answer: str = Field(min_length=1, max_length=12_000)
    citations: list[Citation] = Field(default_factory=list)
    completed_actions: list[str] = Field(default_factory=list)
    proposed_actions: list[str] = Field(default_factory=list)


@dataclass(slots=True)
class CompositionResult:
    output: FinalOutput
    unsupported: list[str] = field(default_factory=list)
    used_model: bool = False


class ResponseComposer:
    def __init__(self, model: ModelService | None = None) -> None:
        self.model = model

    async def compose(
        self,
        state: AgentState,
        *,
        verification: VerificationResult,
        tools: list[Any],
        registry: ToolRegistry,
    ) -> CompositionResult:
        succeeded_ids = {
            result.execution_id for result in state.tool_results if result.status == "succeeded"
        }
        if self.model is not None and self.model.available:
            try:
                draft = await self._compose_with_model(state, verification, tools)
                unsupported = self._validate_draft(draft, state, succeeded_ids)
                if not unsupported:
                    return CompositionResult(
                        output=self._to_output(draft, state),
                        used_model=True,
                    )
                fallback = self._deterministic(state, verification, registry)
                fallback.unsupported.extend(unsupported)
                return fallback
            except LLMError:
                fallback = self._deterministic(state, verification, registry)
                fallback.unsupported.append("composer_model_failed")
                return fallback
        return self._deterministic(state, verification, registry)

    async def _compose_with_model(
        self, state: AgentState, verification: VerificationResult, tools: list[Any]
    ) -> ResponseDraft:
        assert self.model is not None
        system, messages = build_prompt(state, tools=tools)
        context = (
            f"\n\n<verification>{json.dumps(verification.model_dump(mode='json'))}</verification>\n"
            f"<tool_results>{json.dumps([r.model_dump(mode='json') for r in state.tool_results])}"
            "</tool_results>"
        )
        messages[-1] = {
            "role": "user",
            "content": messages[-1]["content"] + context,
        }
        return await self.model.complete_json(
            system=f"{system}\n\n{composer_system_prompt()}",
            messages=[LLMMessage(role=item["role"], content=item["content"]) for item in messages],
            model=ResponseDraft,
            max_tokens=1600,
            temperature=0.2,
        )

    def _validate_draft(
        self, draft: ResponseDraft, state: AgentState, succeeded_ids: set[Any]
    ) -> list[str]:
        issues: list[str] = []
        memory_ids = {memory.id for memory in (state.retrieved.memories if state.retrieved else [])}
        for citation in draft.citations:
            if citation.type == "memory":
                try:
                    if UUID(citation.id) not in memory_ids:
                        issues.append(f"unknown memory citation {citation.id}")
                except ValueError:
                    issues.append(f"invalid memory citation {citation.id}")
            elif citation.type == "tool_execution":
                try:
                    if UUID(citation.id) not in succeeded_ids:
                        issues.append(f"unsuccessful execution citation {citation.id}")
                except ValueError:
                    issues.append(f"invalid execution citation {citation.id}")
        succeeded_tools = {
            result.tool for result in state.tool_results if result.status == "succeeded"
        }
        if draft.completed_actions and not succeeded_tools:
            issues.append("completed actions were claimed without a successful execution")
        if state.approvals.pending and draft.completed_actions:
            issues.append("completed actions were claimed while an approval is pending")
        return issues

    def _to_output(self, draft: ResponseDraft, state: AgentState) -> FinalOutput:
        return FinalOutput(
            text=draft.answer,
            citations=draft.citations,
            execution_summary=draft.completed_actions,
            proposed_actions=draft.proposed_actions,
            usage=state.usage.as_dict(),
        )

    def _deterministic(
        self, state: AgentState, verification: VerificationResult, registry: ToolRegistry
    ) -> CompositionResult:
        lines: list[str] = []
        citations: list[Citation] = []
        summary: list[str] = []
        proposed: list[str] = []

        memories = state.retrieved.memories if state.retrieved else []
        if memories:
            if state.routing and state.routing.intent.label in {
                "search_memory",
                "answer_question",
                "daily_brief",
                "track_goal",
            }:
                lines.append("Here is what I found in your stored memory:")
            else:
                lines.append("Relevant context from your stored memory:")
            for memory in memories[:5]:
                label = memory.title or memory.content[:80]
                lines.append(f"- {memory.content}")
                citations.append(Citation(type="memory", id=str(memory.id), label=label[:160]))

        pending = [r for r in state.tool_results if r.status == "pending_approval"]
        succeeded = [r for r in state.tool_results if r.status == "succeeded"]
        failed = [r for r in state.tool_results if r.status in {"failed", "timed_out", "denied"}]
        for result in succeeded:
            summary.append(f"{result.tool} completed and was verified.")
            citations.append(
                Citation(
                    type="tool_execution",
                    id=str(result.execution_id),
                    label=result.tool,
                )
            )
            detail = self._summarize_output(result)
            if detail:
                lines.append(f"- {result.tool}: {detail}")
        for result in pending:
            proposed.append(result.tool)
            lines.append(
                f"- I prepared {result.tool}, but it has not run. It is waiting for your approval."
            )
        for result in failed:
            lines.append(
                f"- {result.tool} did not complete (status: {result.status}). "
                "I am reporting the actual state, not a success."
            )

        blocked_steps = (
            [step for step in state.plan.steps if step.status == "blocked"] if state.plan else []
        )
        failed_steps = (
            [step for step in state.plan.steps if step.status == "failed"] if state.plan else []
        )

        if not memories and not state.tool_results and not blocked_steps and not failed_steps:
            lines.append(
                "I do not have enough verified information to answer that yet. "
                "I can search memory or connect an integration if you want me to."
            )
        elif verification.issues and not succeeded and not pending:
            lines.append("Verification did not pass, so I am not claiming the task was completed.")
            for issue in verification.issues[:4]:
                lines.append(f"- {issue}")

        for step in failed_steps:
            lines.append(
                f"- Step {step.id} ({step.tool or step.description}) failed and was not "
                "reported as completed."
            )
        for step in blocked_steps:
            proposed.append(step.description)
            lines.append(f"- Step {step.id} could not run: {step.description}.")

        answer = "\n".join(lines).strip()
        if not answer:
            answer = "I could not produce a verified answer for this request."
        return CompositionResult(
            output=FinalOutput(
                text=answer,
                citations=self._dedupe(citations),
                execution_summary=summary,
                proposed_actions=list(dict.fromkeys(proposed)),
                usage=state.usage.as_dict(),
            )
        )

    @staticmethod
    def _summarize_output(result: ToolResultState) -> str:
        output = result.output or {}
        parts: list[str] = []
        for key, value in output.items():
            if isinstance(value, list):
                parts.append(f"{len(value)} {key}")
            elif isinstance(value, (str, int, float, bool)) and value not in ("", None):
                parts.append(f"{key}={str(value)[:100]}")
        return ", ".join(parts[:4])

    @staticmethod
    def _dedupe(citations: list[Citation]) -> list[Citation]:
        seen: set[tuple[str, str]] = set()
        unique: list[Citation] = []
        for citation in citations:
            key = (citation.type, citation.id)
            if key in seen:
                continue
            seen.add(key)
            unique.append(citation)
        return unique


def chunk_text(text: str, *, size: int = 48) -> list[str]:
    """Split composed text into display chunks without changing its content."""
    if size <= 0:
        return [text]
    chunks = [text[index : index + size] for index in range(0, len(text), size)]
    return chunks or [""]


def mentions_unverified_action(text: str) -> bool:
    return bool(
        re.search(
            r"\b(i (?:have )?(?:sent|created|scheduled|shared|deleted|updated)|successfully)\b",
            text,
            re.IGNORECASE,
        )
    )
