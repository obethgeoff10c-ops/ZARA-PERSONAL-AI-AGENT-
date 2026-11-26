"""The single ZARA agent loop: route → retrieve → plan → act → verify → respond."""

from __future__ import annotations

import time
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from zara.agent.composer import ResponseComposer, chunk_text
from zara.agent.planner import Planner, plan_as_dict
from zara.agent.prompts import tool_result_wrapped
from zara.agent.router import ContextRouter
from zara.agent.state import (
    AgentState,
    Domain,
    ErrorRecord,
    FinalOutput,
    IntegrationStatus,
    MemoryCard,
    MessageRef,
    RetrievedContext,
    RunBudget,
    RunStatus,
    SessionContext,
    ToolErrorState,
    ToolRequest,
    ToolResultState,
    TrustLabel,
    UserInput,
)
from zara.agent.verifier import combine_verification, verify_response, verify_tools
from zara.auth.principal import Principal
from zara.config import Settings
from zara.errors import AppError, conflict
from zara.llm.base import LLMError
from zara.llm.service import ModelService, build_provider
from zara.models import AgentRun, Approval, Integration, Message, RunEvent, UserProfile
from zara.services import conversations, runs
from zara.services.audit import record_event
from zara.services.memory.retrieval import RetrievalRequest, RetrievalService
from zara.tools import registry
from zara.tools.executor import ExecutionContext, execute
from zara.tools.spec import ToolSpec

EventSink = Callable[[RunEvent], Awaitable[None]]

_UNSET: Any = object()

_TOOL_DOMAINS: dict[Domain, frozenset[str]] = {
    Domain.PERSONAL: frozenset({"gmail", "calendar", "drive"}),
    Domain.EXECUTIVE: frozenset({"gmail", "calendar", "drive"}),
    Domain.SALES: frozenset({"gmail", "drive"}),
    Domain.DEVELOPER: frozenset({"github"}),
}


class AgentEngine:
    """One orchestrator shared by every domain (no per-domain agents)."""

    def __init__(
        self,
        *,
        provider: Any = _UNSET,
        retrieval: RetrievalService | None = None,
        router: ContextRouter | None = None,
        planner: Planner | None = None,
        composer: ResponseComposer | None = None,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._provider = provider
        self.retrieval = retrieval or RetrievalService()
        self.router = router or ContextRouter()
        self.planner = planner or Planner()
        self.composer = composer or ResponseComposer()
        self._http_client = http_client

    async def run(
        self,
        db: AsyncSession,
        principal: Principal,
        settings: Settings,
        run_id: uuid.UUID,
        *,
        on_event: EventSink | None = None,
    ) -> AgentState:
        run = await runs.get_run(db, principal, run_id)
        if run.status in {"succeeded", "failed", "cancelled", "expired"}:
            raise conflict("This run is already finished.", code="run_terminal")

        provider = build_provider(settings) if self._provider is _UNSET else self._provider
        model = ModelService(provider, settings)
        self.router.model = model
        self.planner.model = model
        self.composer.model = model

        owns_http = self._http_client is None
        http = self._http_client or httpx.AsyncClient(timeout=httpx.Timeout(30.0))
        started = time.monotonic()
        state: AgentState | None = None
        try:
            state = await self._build_state(db, principal, settings, run)
            run.status = RunStatus.RUNNING.value
            run.started_at = run.started_at or datetime.now(UTC)
            run.trace_id = state.session.trace_id
            await db.flush()
            await self._emit(
                db,
                run,
                "run.started",
                {
                    "run_id": str(run.id),
                    "conversation_id": (str(run.conversation_id) if run.conversation_id else None),
                    "user_message_id": (run.trigger_ref or {}).get("user_message_id"),
                },
                on_event,
            )

            await self._route(db, principal, state, run, on_event)
            self._sync_usage(state, model)
            await self._retrieve(db, principal, state, run, on_event)
            await self._plan(state, run, on_event, tools=self._tools_for(state))
            self._sync_usage(state, model)
            await self._act(db, principal, settings, http, state, run, on_event)
            verification = verify_tools(state.plan, state.tool_results)
            state.verification = verification.result

            planner_available = self.planner.model is not None and self.planner.model.available
            should_replan = (
                verification.result.next_action == "replan"
                and state.plan is not None
                and state.plan.version < 2
                and planner_available
            )
            if should_replan:
                await self._replan(
                    state,
                    tools=self._tools_for(state),
                    issues=verification.result.issues,
                )
                await self._emit(
                    db,
                    run,
                    "plan.updated",
                    plan_as_dict(state.plan) if state.plan else {},
                    on_event,
                )
                await self._act(db, principal, settings, http, state, run, on_event)
                verification = verify_tools(state.plan, state.tool_results)
                state.verification = verification.result
            elif verification.result.next_action == "replan":
                # No model planner to produce a different plan: report the actual
                # partial state instead of looping on the same deterministic plan.
                verification.result.next_action = "respond"
                state.verification = verification.result

            if verification.issues:
                state.errors.extend(
                    ErrorRecord(node="verify", kind="validation", message=issue)
                    for issue in verification.issues
                )
            await self._respond(db, principal, state, run, on_event)
            self._sync_usage(state, model)
            await self._finish(db, principal, state, run, started, model, on_event)
            return state
        except AppError as exc:
            await self._fail(db, principal, state, run, started, exc.code, exc.detail, on_event)
            raise
        except LLMError as exc:
            await self._fail(db, principal, state, run, started, exc.code, str(exc), on_event)
            raise
        except Exception as exc:
            await self._fail(
                db,
                principal,
                state,
                run,
                started,
                "internal_error",
                f"{type(exc).__name__}: {exc}",
                on_event,
            )
            raise
        finally:
            if owns_http:
                await http.aclose()

    # --- context -----------------------------------------------------------

    async def _build_state(
        self, db: AsyncSession, principal: Principal, settings: Settings, run: AgentRun
    ) -> AgentState:
        now = datetime.now(UTC)
        trigger_ref = run.trigger_ref or {}
        current_message_id = (
            uuid.UUID(str(trigger_ref["user_message_id"]))
            if trigger_ref.get("user_message_id")
            else None
        )
        profile = (
            await db.execute(
                select(UserProfile).where(
                    UserProfile.tenant_id == principal.tenant_id,
                    UserProfile.user_id == principal.user_id,
                )
            )
        ).scalar_one_or_none()
        integrations = (
            await db.execute(
                select(Integration).where(
                    Integration.tenant_id == principal.tenant_id,
                    Integration.user_id == principal.user_id,
                )
            )
        ).scalars()
        connected = {
            item.provider: IntegrationStatus(
                status=item.status, granted_scopes=list(item.granted_scopes or [])
            )
            for item in integrations
        }
        recent_messages: list[MessageRef] = []
        if run.conversation_id is not None:
            rows = (
                await db.execute(
                    select(Message)
                    .where(
                        Message.tenant_id == principal.tenant_id,
                        Message.user_id == principal.user_id,
                        Message.conversation_id == run.conversation_id,
                    )
                    .order_by(Message.created_at.desc())
                    .limit(10)
                )
            ).scalars()
            recent_messages = [
                MessageRef(role=row.role, content=row.content, created_at=row.created_at)
                for row in reversed(list(rows))
                if row.id != current_message_id
            ]

        budget_values = run.budget or {}
        budget = RunBudget(
            max_steps=int(budget_values.get("max_steps", settings.agent_max_steps)),
            max_tokens=int(budget_values.get("max_tokens", settings.agent_max_tokens)),
            max_cost_usd=float(budget_values.get("max_cost_usd", settings.agent_max_cost_usd)),
            deadline=now + timedelta(seconds=settings.agent_deadline_seconds),
        )
        trace_id = run.trace_id or uuid.uuid4().hex
        return AgentState(
            input=UserInput(
                kind="user_message",
                text=run.goal,
                message_id=current_message_id,
                trust=TrustLabel.USER,
                received_at=now,
            ),
            session=SessionContext(
                tenant_id=principal.tenant_id,
                user_id=principal.user_id,
                run_id=run.id,
                conversation_id=run.conversation_id,
                trace_id=trace_id,
                timezone=profile.timezone if profile else "UTC",
                locale=(profile.locale if profile else None) or "en",
                now=now,
                working_hours=profile.working_hours if profile else {},
                quiet_hours=profile.quiet_hours if profile else {},
                autonomy={
                    domain.value.lower(): run.autonomy_level or "suggest" for domain in Domain
                },
                connected_integrations=connected,
                budget=budget,
                recent_messages=recent_messages,
                profile_summary=self._profile_summary(profile),
            ),
            goal=run.goal,
            status=RunStatus.RUNNING,
        )

    @staticmethod
    def _profile_summary(profile: UserProfile | None) -> str:
        if profile is None:
            return "No profile summary available."
        parts = [
            value
            for value in (
                profile.bio_summary,
                profile.job_title,
                profile.company,
                profile.communication_style,
            )
            if value
        ]
        return " · ".join(str(part) for part in parts) or "No profile summary available."

    # --- nodes -------------------------------------------------------------

    async def _route(
        self,
        db: AsyncSession,
        principal: Principal,
        state: AgentState,
        run: AgentRun,
        on_event: EventSink | None,
    ) -> None:
        await self._check_budget(state)
        recent_domains = await self._recent_domains(db, principal, run)
        state.routing = await self.router.route(
            db, principal, state.input, state.session, recent_domains=recent_domains
        )
        run.routing = state.routing.model_dump(mode="json")
        run.domains = state.routing.lower_domains()
        await db.flush()
        self._persist(state, run)
        await self._emit(
            db,
            run,
            "route.decided",
            {
                "domains": [
                    {"domain": item.domain.value, "score": item.score, "primary": item.primary}
                    for item in state.routing.domains
                    if item.domain in state.routing.active_domains
                ],
                "intent": state.routing.intent.label,
                "confidence": state.routing.confidence,
            },
            on_event,
        )

    async def _retrieve(
        self,
        db: AsyncSession,
        principal: Principal,
        state: AgentState,
        run: AgentRun,
        on_event: EventSink | None,
    ) -> None:
        assert state.routing is not None
        await self._check_budget(state)
        memories, log_id = await self.retrieval.retrieve(
            db,
            principal,
            RetrievalRequest(
                query_text=state.goal or state.input.text or "",
                domains=state.routing.lower_domains(),
                entity_ids=state.routing.retrieval_plan.entity_ids,
                kinds=state.routing.retrieval_plan.kinds or None,
                k=state.routing.retrieval_plan.k,
                token_budget=state.routing.retrieval_plan.token_budget,
                run_id=run.id,
            ),
        )
        state.retrieved = RetrievedContext(
            memories=[
                MemoryCard(
                    id=memory.id,
                    kind=memory.kind,
                    title=memory.title,
                    content=memory.content,
                    score=memory.score,
                    confidence=memory.confidence,
                    sensitivity=memory.sensitivity,
                    sources=memory.sources,
                    due_at=memory.due_at,
                )
                for memory in memories
            ],
            retrieval_log_id=log_id,
        )
        self._persist(state, run)
        await self._emit(
            db,
            run,
            "memory.retrieved",
            {
                "count": len(memories),
                "items": [
                    {
                        "id": str(memory.id),
                        "label": memory.title or memory.content[:80],
                        "kind": memory.kind,
                    }
                    for memory in memories
                ],
            },
            on_event,
        )

    async def _plan(
        self,
        state: AgentState,
        run: AgentRun,
        on_event: EventSink | None,
        *,
        tools: list[ToolSpec],
    ) -> None:
        await self._check_budget(state)
        result = await self.planner.create_plan(
            state,
            tools=tools,
            registry=registry,
            max_steps=state.session.budget.max_steps,
        )
        state.plan = result.plan
        for issue in result.issues:
            state.errors.append(ErrorRecord(node="plan", kind="validation", message=issue))
        self._persist(state, run)
        _ = on_event

    async def _act(
        self,
        db: AsyncSession,
        principal: Principal,
        settings: Settings,
        http: httpx.AsyncClient,
        state: AgentState,
        run: AgentRun,
        on_event: EventSink | None,
    ) -> None:
        if state.plan is not None:
            event_type = "plan.created" if state.plan.version == 1 else "plan.updated"
            await self._emit(db, run, event_type, plan_as_dict(state.plan), on_event)
        if state.plan is None:
            return
        domains = self._tool_domains(state)
        connected = {
            provider
            for provider, status in state.session.connected_integrations.items()
            if status.status == "active"
        }
        for step in state.plan.steps:
            await self._check_budget(state)
            if step.status == "blocked":
                continue
            if step.tool is None:
                step.status = "done"
                continue
            dependency_missing = any(
                (dependency := state.plan.step(dep)) is None or dependency.status != "done"
                for dep in step.depends_on
            )
            if dependency_missing:
                step.status = "skipped"
                continue
            spec = registry.get(step.tool)
            if spec is None or (spec.integration is not None and spec.integration not in connected):
                step.status = "blocked"
                state.errors.append(
                    ErrorRecord(
                        node="act",
                        kind="integration",
                        message=f"tool {step.tool} requires a connected integration",
                        step_id=step.id,
                    )
                )
                continue

            step.status = "running"
            step_id = uuid.uuid5(run.id, step.id)
            request = ToolRequest(
                request_id=uuid.uuid4().hex,
                tool=spec.name,
                args=dict(step.args),
                derived_from=[TrustLabel.USER],
                idempotency_key=f"{run.id}:{step.id}",
                plan_step_id=step.id,
            )
            state.tool_requests.append(request)
            await self._emit(
                db,
                run,
                "tool.started",
                {
                    "execution_id": None,
                    "tool": spec.name,
                    "tier": spec.risk_tier,
                    "args_preview": _redacted_args(spec, step.args),
                },
                on_event,
            )
            started = time.monotonic()
            context = ExecutionContext(
                principal=principal,
                settings=settings,
                http=http,
                autonomy=run.autonomy_level or "suggest",
                domains=frozenset(domains),
                run_id=run.id,
                step_id=step_id,
                run_tainted=state.taint == "tainted",
                budget_exceeded=False,
                kill_switches=frozenset(state.session.kill_switches),
            )
            try:
                result = await execute(
                    db,
                    principal,
                    settings,
                    context,
                    tool_name=spec.name,
                    raw_args=step.args,
                )
            except AppError as exc:
                step.status = "failed"
                state.errors.append(
                    ErrorRecord(
                        node="act",
                        kind="tool",
                        message=f"{exc.code}: {exc.detail}",
                        retryable=False,
                        step_id=step.id,
                    )
                )
                self._persist(state, run)
                continue
            latency_ms = int((time.monotonic() - started) * 1000)
            state.usage.tool_calls += 1
            output = result.output.model_dump(mode="json") if result.output is not None else None
            wrapped = (
                tool_result_wrapped(spec.name, str(result.execution_id), output)
                if output is not None
                else None
            )
            status = (
                result.status
                if result.status
                in {
                    "succeeded",
                    "failed",
                    "denied",
                    "timed_out",
                    "pending_approval",
                }
                else "failed"
            )
            observation = ToolResultState(
                request_id=request.request_id,
                execution_id=result.execution_id,
                tool=spec.name,
                status=status,
                output=output,
                output_text_wrapped=wrapped,
                error=(
                    ToolErrorState(kind="tool", message=result.reason)
                    if result.reason and status != "pending_approval"
                    else None
                ),
                approval_id=result.approval_id,
                latency_ms=latency_ms,
                trust=(
                    TrustLabel.EXTERNAL if spec.output_trust == "untrusted" else TrustLabel.ZARA
                ),
            )
            state.tool_results.append(observation)
            if status == "succeeded":
                step.status = "done"
                step.result_ref = str(result.execution_id)
                if spec.output_trust == "untrusted":
                    state.taint = "tainted"
            elif status == "pending_approval":
                step.status = "blocked"
                step.approval_id = result.approval_id
                if result.approval_id is not None:
                    state.approvals.pending.append(result.approval_id)
                await self._emit(
                    db,
                    run,
                    "approval.requested",
                    {
                        "approval_id": str(result.approval_id) if result.approval_id else None,
                        "tool": spec.name,
                        "tier": spec.risk_tier,
                        "preview": await self._approval_preview(db, principal, result.approval_id),
                        "expires_at": None,
                    },
                    on_event,
                )
            else:
                step.status = "failed"
                state.errors.append(
                    ErrorRecord(
                        node="act",
                        kind="tool",
                        message=f"{spec.name} ended with status {status}",
                        step_id=step.id,
                    )
                )
            self._persist(state, run)
            await self._emit(
                db,
                run,
                "tool.finished",
                {
                    "execution_id": str(result.execution_id),
                    "status": status,
                    "summary": _result_summary(observation),
                    "latency_ms": latency_ms,
                },
                on_event,
            )
            if status == "denied":
                await self._emit(
                    db,
                    run,
                    "tool.denied",
                    {
                        "execution_id": str(result.execution_id),
                        "tool": spec.name,
                        "reason": result.reason,
                    },
                    on_event,
                )
            if status == "pending_approval":
                # Stop before starting another action while a decision is pending.
                break

    async def _replan(
        self,
        state: AgentState,
        *,
        tools: list[ToolSpec],
        issues: list[str],
    ) -> None:
        if state.plan is None:
            return
        previous = state.plan
        result = await self.planner.create_plan(
            state,
            tools=tools,
            registry=registry,
            max_steps=state.session.budget.max_steps,
            revision=2,
            verification_issues=issues,
        )
        result.plan.version = 2
        state.plan = result.plan
        state.scratch.append(
            {"event": "replan", "previous_version": previous.version, "issues": issues}
        )
        if [step.tool for step in result.plan.steps] == [step.tool for step in previous.steps]:
            state.errors.append(
                ErrorRecord(
                    node="verify",
                    kind="validation",
                    message="replan produced no material change",
                )
            )

    async def _respond(
        self,
        db: AsyncSession,
        principal: Principal,
        state: AgentState,
        run: AgentRun,
        on_event: EventSink | None,
    ) -> None:
        assert state.verification is not None
        tools = self._tools_for(state)
        composition = await self.composer.compose(
            state, verification=state.verification, tools=tools, registry=registry
        )
        succeeded_ids = {
            result.execution_id for result in state.tool_results if result.status == "succeeded"
        }
        allowed_memory_ids = {
            memory.id for memory in (state.retrieved.memories if state.retrieved else [])
        }
        unsupported = verify_response(
            composition.output.text,
            composition.output.citations,
            state=state,
            allowed_memory_ids=allowed_memory_ids,
            succeeded_execution_ids=succeeded_ids,
        )
        if unsupported and composition.used_model:
            composition = await ResponseComposer(None).compose(
                state, verification=state.verification, tools=tools, registry=registry
            )
            unsupported = verify_response(
                composition.output.text,
                composition.output.citations,
                state=state,
                allowed_memory_ids=allowed_memory_ids,
                succeeded_execution_ids=succeeded_ids,
            )
        if composition.unsupported:
            unsupported.extend(composition.unsupported)
        state.verification = combine_verification(
            state.verification, unsupported, composition.output.citations
        )
        state.final_output = composition.output
        state.final_output.usage = state.usage.as_dict()
        for chunk in chunk_text(composition.output.text):
            await self._emit(db, run, "token", {"delta": chunk, "block_index": 0}, on_event)
        for citation in composition.output.citations:
            await self._emit(
                db,
                run,
                "citation",
                {"block_index": 0, "ref": citation.model_dump(mode="json")},
                on_event,
            )
        message_id: str | None = None
        if run.conversation_id is not None:
            message = await conversations.append_assistant_message(
                db,
                principal,
                run.conversation_id,
                content=composition.output.text,
                run_id=run.id,
                citations=[
                    citation.model_dump(mode="json") for citation in composition.output.citations
                ],
            )
            message_id = str(message.id)
        self._persist(state, run)
        await self._emit(
            db,
            run,
            "message.completed",
            {
                "message_id": message_id,
                "content": composition.output.text,
                "citations": [
                    citation.model_dump(mode="json") for citation in composition.output.citations
                ],
                "usage": {
                    "tokens_in": state.usage.tokens_in,
                    "tokens_out": state.usage.tokens_out,
                },
            },
            on_event,
        )

    async def _finish(
        self,
        db: AsyncSession,
        principal: Principal,
        state: AgentState,
        run: AgentRun,
        started: float,
        model: ModelService,
        on_event: EventSink | None,
    ) -> None:
        self._sync_usage(state, model)
        if state.approvals.pending:
            run.status = RunStatus.AWAITING_APPROVAL.value
        elif state.verification and (
            state.verification.next_action == "fail" or state.verification.goal_satisfied is False
        ):
            run.status = RunStatus.FAILED.value
            run.finished_at = datetime.now(UTC)
            run.error = {
                "code": "verification_failed",
                "message": "; ".join(state.verification.issues[:5]) or "Verification failed",
            }
        else:
            run.status = RunStatus.SUCCEEDED.value
            run.finished_at = datetime.now(UTC)
        state.status = RunStatus(run.status)
        run.usage = state.usage.as_dict()
        if state.final_output is not None:
            state.final_output.usage = state.usage.as_dict()
            run.final_output = state.final_output.model_dump(mode="json")
        run.state_snapshot = state.compact_snapshot()
        await db.flush()
        await record_event(
            db,
            principal,
            action="run.completed",
            resource_type="agent_run",
            resource_id=str(run.id),
            run_id=run.id,
            metadata={
                "status": run.status,
                "domains": run.domains,
                "tool_calls": state.usage.tool_calls,
                "duration_ms": int((time.monotonic() - started) * 1000),
            },
        )
        await self._emit(db, run, "run.status", {"status": run.status}, on_event)

    async def _fail(
        self,
        db: AsyncSession,
        principal: Principal,
        state: AgentState | None,
        run: AgentRun,
        started: float,
        code: str,
        message: str,
        on_event: EventSink | None,
    ) -> None:
        run.status = RunStatus.FAILED.value
        run.finished_at = datetime.now(UTC)
        run.error = {"code": code, "message": message[:1000]}
        if state is not None:
            state.status = RunStatus.FAILED
            state.errors.append(
                ErrorRecord(
                    node="run",
                    kind="internal" if code == "internal_error" else "validation",
                    message=message[:1000],
                )
            )
            state.final_output = state.final_output or FinalOutput(
                text=(
                    "I could not finish this run. "
                    f"Actual state: failed ({code}). Nothing else was claimed as completed."
                )
            )
            state.final_output.usage = state.usage.as_dict()
            run.usage = state.usage.as_dict()
            run.final_output = state.final_output.model_dump(mode="json")
            run.state_snapshot = state.compact_snapshot()
        await db.flush()
        await self._emit(
            db,
            run,
            "error",
            {"code": code, "message": message[:500], "retryable": code != "budget_exceeded"},
            on_event,
        )
        await self._emit(db, run, "run.status", {"status": run.status}, on_event)
        await record_event(
            db,
            principal,
            action="run.failed",
            resource_type="agent_run",
            resource_id=str(run.id),
            run_id=run.id,
            metadata={"code": code, "duration_ms": int((time.monotonic() - started) * 1000)},
        )

    # --- helpers -----------------------------------------------------------

    def _tools_for(self, state: AgentState) -> list[ToolSpec]:
        domains = self._tool_domains(state)
        connected = {
            provider
            for provider, status in state.session.connected_integrations.items()
            if status.status == "active"
        }
        return [
            spec
            for spec in registry.expose(domains)
            if spec.integration is None or spec.integration in connected
        ]

    @staticmethod
    def _tool_domains(state: AgentState) -> set[str]:
        domains: set[str] = set()
        if state.routing is None:
            return set(_TOOL_DOMAINS[Domain.PERSONAL])
        for domain in state.routing.active_domains:
            domains.update(_TOOL_DOMAINS.get(domain, frozenset()))
        return domains or set(_TOOL_DOMAINS[Domain.PERSONAL])

    async def _recent_domains(
        self, db: AsyncSession, principal: Principal, run: AgentRun
    ) -> list[Domain]:
        if run.conversation_id is None:
            return []
        rows = (
            await db.execute(
                select(AgentRun.domains)
                .where(
                    AgentRun.tenant_id == principal.tenant_id,
                    AgentRun.user_id == principal.user_id,
                    AgentRun.conversation_id == run.conversation_id,
                    AgentRun.id != run.id,
                    AgentRun.domains.is_not(None),
                )
                .order_by(AgentRun.created_at.desc())
                .limit(3)
            )
        ).scalars()
        domains: list[Domain] = []
        for values in rows:
            for value in values or []:
                try:
                    domain = Domain(str(value).upper())
                except ValueError:
                    continue
                if domain not in domains:
                    domains.append(domain)
        return domains

    async def _approval_preview(
        self, db: AsyncSession, principal: Principal, approval_id: uuid.UUID | None
    ) -> dict[str, Any] | None:
        if approval_id is None:
            return None
        approval = (
            await db.execute(
                select(Approval).where(
                    Approval.id == approval_id,
                    Approval.tenant_id == principal.tenant_id,
                    Approval.user_id == principal.user_id,
                )
            )
        ).scalar_one_or_none()
        if approval is None:
            return None
        return {
            key: value for key, value in approval.preview.items() if not str(key).startswith("_")
        }

    @staticmethod
    def _persist(state: AgentState, run: AgentRun) -> None:
        run.state_snapshot = state.compact_snapshot()
        run.usage = state.usage.as_dict()

    @staticmethod
    def _sync_usage(state: AgentState, model: ModelService) -> None:
        state.usage.add_model(model.usage_snapshot())

    async def _emit(
        self,
        db: AsyncSession,
        run: AgentRun,
        event_type: str,
        data: dict[str, Any],
        on_event: EventSink | None,
    ) -> None:
        event = await runs.append_event(db, run, event_type, data)
        if on_event is not None:
            await on_event(event)

    async def _check_budget(self, state: AgentState) -> None:
        state.step_count += 1
        budget = state.session.budget
        if state.step_count > budget.max_steps:
            raise AppError(
                "The run exceeded its step budget.",
                status_code=402,
                code="budget_exceeded",
                title="Budget exceeded",
            )
        if state.usage.tokens_in + state.usage.tokens_out > budget.max_tokens:
            raise AppError(
                "The run exceeded its token budget.",
                status_code=402,
                code="budget_exceeded",
                title="Budget exceeded",
            )
        if state.usage.cost_usd > budget.max_cost_usd:
            raise AppError(
                "The run exceeded its cost budget.",
                status_code=402,
                code="budget_exceeded",
                title="Budget exceeded",
            )
        if datetime.now(UTC) > budget.deadline:
            raise AppError(
                "The run exceeded its deadline.",
                status_code=402,
                code="budget_exceeded",
                title="Budget exceeded",
            )


def _redacted_args(spec: ToolSpec, args: dict[str, Any]) -> dict[str, Any]:
    redacted = dict(args)
    for key in spec.redact_args:
        if key.startswith("$."):
            redacted.pop(key[2:], None)
    for key in spec.critical_args:
        if key in redacted and isinstance(redacted[key], str):
            redacted[key] = redacted[key][:80]
    return redacted


def _result_summary(result: ToolResultState) -> dict[str, Any]:
    output = result.output or {}
    summary: dict[str, Any] = {"tool": result.tool, "status": result.status}
    for key, value in output.items():
        if isinstance(value, list):
            summary[f"{key}_count"] = len(value)
    return summary
