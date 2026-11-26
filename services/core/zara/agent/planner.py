"""Structured planner with strict tool validation (04 §1, 05 §2)."""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, Field, ValidationError

from zara.agent.prompts import planner_system_prompt
from zara.agent.state import (
    AgentState,
    Plan,
    PlanStep,
    RetrievedContext,
)
from zara.llm.base import LLMError, LLMMessage
from zara.llm.service import ModelService
from zara.tools.registry import ToolRegistry
from zara.tools.spec import ToolSpec

_ID_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9_-]{0,31}$")


class PlanStepDraft(BaseModel):
    id: str
    description: str = Field(min_length=1, max_length=500)
    tool: str | None = None
    args: dict[str, Any] = Field(default_factory=dict)
    depends_on: list[str] = Field(default_factory=list)


class PlanDraft(BaseModel):
    goal: str = Field(min_length=1, max_length=2000)
    steps: list[PlanStepDraft] = Field(min_length=1, max_length=12)
    rationale: str = Field(default="", max_length=2000)
    parallel_groups: list[list[str]] = Field(default_factory=list)


@dataclass(slots=True)
class PlanBuildResult:
    plan: Plan
    issues: list[str] = field(default_factory=list)


class Planner:
    def __init__(self, model: ModelService | None = None) -> None:
        self.model = model

    async def create_plan(
        self,
        state: AgentState,
        *,
        tools: list[ToolSpec],
        registry: ToolRegistry,
        max_steps: int,
        revision: int = 1,
        verification_issues: list[str] | None = None,
    ) -> PlanBuildResult:
        if self.model is not None and self.model.available:
            try:
                draft = await self._plan_with_model(
                    state,
                    tools=tools,
                    max_steps=max_steps,
                    revision=revision,
                    verification_issues=verification_issues or [],
                )
                return self._validate(draft, registry=registry, allowed=tools, max_steps=max_steps)
            except LLMError:
                # A model failure must not fabricate a plan; use the honest
                # deterministic plan and record the fallback in the rationale.
                result = self._fallback_plan(state, tools=tools, max_steps=max_steps)
                result.issues.append("planner_model_failed")
                result.plan.rationale = (
                    f"{result.plan.rationale} (model planner unavailable; deterministic fallback)"
                )
                return result
        return self._fallback_plan(state, tools=tools, max_steps=max_steps)

    async def _plan_with_model(
        self,
        state: AgentState,
        *,
        tools: list[ToolSpec],
        max_steps: int,
        revision: int,
        verification_issues: list[str],
    ) -> PlanDraft:
        assert self.model is not None
        assert state.routing is not None
        tool_table = [
            {
                "name": tool.name,
                "description": tool.description,
                "risk_tier": tool.risk_tier,
                "side_effect": tool.side_effect,
                "input_schema": tool.input_model.model_json_schema(),
            }
            for tool in tools
        ]
        memory_lines = [
            f"[{memory.id}] {memory.kind}: {memory.content}"
            for memory in (state.retrieved or RetrievedContext()).memories[:12]
        ]
        user = (
            f"Goal: {state.goal}\n"
            f"Active domains: {[d.value for d in state.routing.active_domains]}\n"
            f"Intent: {state.routing.intent.model_dump()}\n"
            f"Retrieved memory:\n" + "\n".join(memory_lines) + "\n"
            f"Available tools:\n{json.dumps(tool_table, default=str)}\n"
            f"Max steps: {max_steps}\n"
            f"Plan revision: {revision}\n"
            f"Verification issues to address: {verification_issues}\n"
            "Use tool names exactly as listed. If no tool is needed, use one step with tool=null."
        )
        return await self.model.complete_json(
            system=planner_system_prompt(),
            messages=[LLMMessage(role="user", content=user)],
            model=PlanDraft,
            max_tokens=1600,
        )

    def _validate(
        self,
        draft: PlanDraft,
        *,
        registry: ToolRegistry,
        allowed: list[ToolSpec],
        max_steps: int,
    ) -> PlanBuildResult:
        issues: list[str] = []
        allowed_by_name = {tool.name: tool for tool in allowed}
        steps: list[PlanStep] = []
        seen: set[str] = set()
        for index, item in enumerate(draft.steps[:max_steps], start=1):
            step_id = item.id if _ID_RE.match(item.id) else f"s{index}"
            if step_id in seen:
                issues.append(f"duplicate step id {step_id!r} replaced")
                step_id = f"s{index}"
            seen.add(step_id)
            if item.tool is None:
                steps.append(
                    PlanStep(
                        id=step_id,
                        description=item.description,
                        depends_on=list(dict.fromkeys(item.depends_on)),
                        status="pending",
                    )
                )
                continue
            spec = allowed_by_name.get(item.tool)
            if spec is None:
                # Unknown or unexposed tools are blocked, never executed.
                issues.append(f"unknown or unexposed tool {item.tool!r} blocked")
                steps.append(
                    PlanStep(
                        id=step_id,
                        description=item.description,
                        tool=item.tool,
                        args=item.args,
                        depends_on=list(dict.fromkeys(item.depends_on)),
                        status="blocked",
                    )
                )
                continue
            try:
                validated_args = spec.input_model.model_validate(item.args)
            except ValidationError as exc:
                issues.append(
                    f"step {step_id} arguments invalid: "
                    + "; ".join(error["msg"] for error in exc.errors())
                )
                steps.append(
                    PlanStep(
                        id=step_id,
                        description=item.description,
                        tool=item.tool,
                        args=item.args,
                        depends_on=list(dict.fromkeys(item.depends_on)),
                        risk_tier=spec.risk_tier,
                        status="blocked",
                        requires_approval=spec.risk_tier >= 3,
                    )
                )
                continue
            steps.append(
                PlanStep(
                    id=step_id,
                    description=item.description,
                    tool=spec.name,
                    args=validated_args.model_dump(),
                    depends_on=list(dict.fromkeys(item.depends_on)),
                    risk_tier=spec.risk_tier,
                    requires_approval=spec.risk_tier >= 3,
                )
            )
        known_ids = {step.id for step in steps}
        for step in steps:
            step.depends_on = [
                dep for dep in step.depends_on if dep in known_ids and dep != step.id
            ]
        groups = self._parallel_groups(steps)
        if draft.parallel_groups:
            requested = {frozenset(group) for group in draft.parallel_groups}
            computed = {frozenset(group) for group in groups}
            if not requested <= computed:
                issues.append("planner parallel groups adjusted to dependency-safe groups")
        return PlanBuildResult(
            plan=Plan(
                goal=draft.goal or "Complete the request",
                steps=steps,
                rationale=draft.rationale,
                parallel_groups=groups,
            ),
            issues=issues,
        )

    def _fallback_plan(
        self, state: AgentState, *, tools: list[ToolSpec], max_steps: int
    ) -> PlanBuildResult:
        goal = state.goal or "Respond to the user"
        text = (state.input.text or "").strip()
        steps: list[PlanStep] = []
        spec = self._explicit_tool(state, tools)
        if spec is not None:
            args = self._explicit_args(spec, text)
            if args is not None:
                steps.append(
                    PlanStep(
                        id="s1",
                        description=f"Use {spec.name}",
                        tool=spec.name,
                        args=args,
                        risk_tier=spec.risk_tier,
                        requires_approval=spec.risk_tier >= 3,
                    )
                )
        if not steps:
            steps.append(
                PlanStep(
                    id="s1",
                    description="Answer from verified memory and tool context",
                )
            )
        if len(steps) > max_steps:
            steps = steps[:max_steps]
        return PlanBuildResult(
            plan=Plan(
                goal=goal,
                steps=steps,
                rationale="Deterministic plan: no model planner is configured.",
                parallel_groups=self._parallel_groups(steps),
            )
        )

    @staticmethod
    def _explicit_tool(state: AgentState, tools: list[ToolSpec]) -> ToolSpec | None:
        text = (state.input.text or "").lower()
        by_name = {tool.name: tool for tool in tools}
        if re.search(r"\b(search|find)\b.{0,40}\b(email|mail|inbox)\b", text):
            return by_name.get("gmail.search_threads")
        if re.search(r"\b(what'?s on|show|list)\b.{0,30}\bcalendar\b", text):
            return by_name.get("gcal.list_events")
        if re.search(r"\b(list|show)\b.{0,30}\b(pull request|prs?)\b", text):
            return by_name.get("github.list_pull_requests")
        if re.search(r"\b(search|find)\b.{0,30}\b(drive|files?|docs?)\b", text):
            return by_name.get("drive.search_files")
        return None

    @staticmethod
    def _explicit_args(spec: ToolSpec, text: str) -> dict[str, Any] | None:
        if spec.name == "gmail.search_threads":
            match = re.search(r"\b(?:for|about|mentioning)\s+(.+)$", text, re.I)
            return {"query": (match.group(1) if match else text)[:500]}
        if spec.name == "gcal.list_events":
            from datetime import UTC, datetime, timedelta

            now = datetime.now(UTC)
            return {
                "time_min": now.isoformat(),
                "time_max": (now + timedelta(days=7)).isoformat(),
            }
        if spec.name == "drive.search_files":
            match = re.search(r"\b(?:for|about|named)\s+(.+)$", text, re.I)
            return {"query": (match.group(1) if match else text)[:500]}
        return None

    @staticmethod
    def _parallel_groups(steps: list[PlanStep]) -> list[list[str]]:
        levels: dict[str, int] = {}
        by_id = {step.id: step for step in steps}
        visiting: set[str] = set()

        def level(step_id: str) -> int:
            if step_id in levels:
                return levels[step_id]
            if step_id in visiting:
                return 0
            visiting.add(step_id)
            step = by_id[step_id]
            value = 0
            for dependency in step.depends_on:
                if dependency in by_id:
                    value = max(value, level(dependency) + 1)
            visiting.discard(step_id)
            levels[step_id] = value
            return value

        for step in steps:
            level(step.id)
        grouped: dict[int, list[str]] = {}
        for step in steps:
            step.parallel_group = levels.get(step.id, 0)
            grouped.setdefault(step.parallel_group, []).append(step.id)
        return [grouped[index] for index in sorted(grouped)]


def plan_as_dict(plan: Plan) -> dict[str, Any]:
    return {
        "version": plan.version,
        "goal": plan.goal,
        "rationale": plan.rationale,
        "parallel_groups": plan.parallel_groups,
        "steps": [
            {
                "id": step.id,
                "description": step.description,
                "tool": step.tool,
                "status": step.status,
                "risk_tier": step.risk_tier,
                "depends_on": step.depends_on,
                "requires_approval": step.requires_approval,
            }
            for step in plan.steps
        ],
    }


def new_request_id() -> str:
    return uuid.uuid4().hex
