"""Unit tests for the Prompt 06 agent intelligence core (no database)."""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from zara.agent.composer import ResponseComposer, chunk_text
from zara.agent.planner import Planner, plan_as_dict
from zara.agent.prompts import sanitize_untrusted, wrap_untrusted
from zara.agent.router import ContextRouter
from zara.agent.state import (
    AgentState,
    Citation,
    Domain,
    MemoryCard,
    Plan,
    PlanStep,
    RetrievedContext,
    RoutingDecision,
    RunBudget,
    SessionContext,
    ToolResultState,
    TrustLabel,
    UserInput,
)
from zara.agent.verifier import combine_verification, verify_response, verify_tools
from zara.auth.principal import Principal
from zara.config import Settings
from zara.llm.anthropic import AnthropicProvider
from zara.llm.base import LLMDelta, LLMError, LLMMessage, LLMResponse, LLMUsage
from zara.llm.service import ModelService, build_provider
from zara.tools import registry

# --- fixtures / doubles -----------------------------------------------------


class _FakeScalars:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def __iter__(self) -> Any:
        return iter(self._rows)


class _FakeResult:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def scalars(self) -> _FakeScalars:
        return _FakeScalars(self._rows)


class _FakeDb:
    async def execute(self, *_args: Any, **_kwargs: Any) -> _FakeResult:
        return _FakeResult([])


class ScriptedProvider:
    """Deterministic provider keyed by a marker in the system prompt."""

    def __init__(self, responses: dict[str, str]) -> None:
        self._responses = responses
        self.calls: list[str] = []
        self._model = "scripted-model"

    @property
    def model(self) -> str:
        return self._model

    async def complete(
        self,
        *,
        system: str,
        messages: Sequence[LLMMessage],
        max_tokens: int,
        temperature: float = 0.0,
        response_schema: dict[str, Any] | None = None,
    ) -> LLMResponse:
        self.calls.append(system)
        for marker, text in self._responses.items():
            if marker in system:
                return LLMResponse(text=text, usage=LLMUsage(12, 6), model=self._model)
        return LLMResponse(text="{}", usage=LLMUsage(1, 1), model=self._model)

    async def stream(
        self,
        *,
        system: str,
        messages: Sequence[LLMMessage],
        max_tokens: int,
        temperature: float = 0.0,
    ) -> AsyncIterator[LLMDelta]:
        self.calls.append(system)
        yield LLMDelta(text="hello")
        yield LLMDelta(text=" world", usage=LLMUsage(2, 3), stop_reason="end_turn")


def _principal() -> Principal:
    return Principal(
        tenant_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        role="member",
        email="agent@example.com",
        display_name=None,
        auth_provider="dev",
        external_auth_id="dev",
    )


def _settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "_env_file": None,
        "database_url": "postgresql+asyncpg://zara:zara_local_dev@localhost:5432/zara",
        "redis_url": None,
    }
    values.update(overrides)
    return Settings(**values)


def _session(*, recent: list[str] | None = None) -> SessionContext:
    now = datetime.now(UTC)
    from zara.agent.state import MessageRef

    return SessionContext(
        tenant_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        run_id=uuid.uuid4(),
        trace_id="trace",
        now=now,
        budget=RunBudget(deadline=now + timedelta(seconds=60)),
        recent_messages=[MessageRef(role="user", content=text) for text in (recent or [])],
    )


def _state(text: str, *, intent: str = "answer_question") -> AgentState:
    from zara.agent.state import Intent

    state = AgentState(
        input=UserInput(kind="user_message", text=text, trust=TrustLabel.USER),
        session=_session(),
        goal=text,
    )
    state.routing = RoutingDecision(
        domains=[],
        active_domains=[Domain.PERSONAL],
        intent=Intent(label=intent, confidence=0.9),
    )
    return state


async def _route(text: str) -> RoutingDecision:
    router = ContextRouter()
    return await router.route(
        _FakeDb(),  # type: ignore[arg-type]
        _principal(),
        UserInput(kind="user_message", text=text, trust=TrustLabel.USER),
        _session(),
    )


# --- router -----------------------------------------------------------------


async def test_intent_detection_and_plan_flag() -> None:
    decision = await _route("Remind me to call the dentist tomorrow")
    assert decision.intent.label == "set_reminder"
    assert decision.intent.needs_plan is True
    assert decision.intent.write_likely is True


async def test_daily_brief_routes_personal_and_executive() -> None:
    decision = await _route("What should I do today?")
    assert set(decision.active_domains) == {Domain.PERSONAL, Domain.EXECUTIVE}


async def test_meeting_with_prospect_routes_three_domains() -> None:
    decision = await _route("Prepare me for my meeting with this prospect")
    assert set(decision.active_domains) == {
        Domain.EXECUTIVE,
        Domain.SALES,
        Domain.PERSONAL,
    }


async def test_github_deadline_routes_developer_executive_sales() -> None:
    decision = await _route("Is the GitHub bug affecting the client's deadline?")
    assert set(decision.active_domains) == {
        Domain.DEVELOPER,
        Domain.EXECUTIVE,
        Domain.SALES,
    }
    assert decision.intent.label == "check_pr_status"


async def test_preference_recall_routes_personal() -> None:
    decision = await _route("Remind me of the approach I preferred last time")
    assert decision.active_domains == [Domain.PERSONAL]
    assert "preference" in decision.retrieval_plan.kinds


async def test_router_merges_model_domains_when_available() -> None:
    draft = {
        "domains": [
            {"domain": "DEVELOPER", "score": 0.9, "reasons": ["ci failing"]},
            {"domain": "SALES", "score": 0.5, "reasons": ["client"]},
        ],
        "intent": {
            "label": "check_pr_status",
            "confidence": 0.9,
            "needs_plan": True,
            "write_likely": False,
        },
        "required_integrations": ["github"],
        "entity_hints": [],
    }
    provider = ScriptedProvider({"context router": json.dumps(draft)})
    router = ContextRouter(model=ModelService(provider, _settings()))
    decision = await router.route(
        _FakeDb(),  # type: ignore[arg-type]
        _principal(),
        UserInput(kind="user_message", text="How is the build doing?", trust=TrustLabel.USER),
        _session(),
    )
    assert Domain.DEVELOPER in decision.active_domains
    assert Domain.SALES in decision.active_domains
    assert "github" in decision.required_integrations


async def test_router_falls_back_when_model_fails() -> None:
    class _Broken:
        @property
        def model(self) -> str:
            return "broken"

        async def complete(self, **_kwargs: Any) -> LLMResponse:
            raise LLMError("boom", code="llm_unavailable", retryable=False)

        async def stream(self, **_kwargs: Any) -> AsyncIterator[LLMDelta]:
            yield LLMDelta(text="")

    router = ContextRouter(model=ModelService(_Broken(), _settings()))
    decision = await router.route(
        _FakeDb(),  # type: ignore[arg-type]
        _principal(),
        UserInput(kind="user_message", text="How is the build doing?", trust=TrustLabel.USER),
        _session(),
    )
    assert decision.fallback_used == "deterministic_only"
    assert decision.active_domains


# --- planner ----------------------------------------------------------------


def _plan_draft(**payload: Any) -> str:
    return json.dumps(payload)


async def _create_plan(provider_text: str) -> Any:
    provider = ScriptedProvider({"planner": provider_text})
    planner = Planner(model=ModelService(provider, _settings()))
    state = _state("Search my email for the Lisbon budget")
    state.retrieved = RetrievedContext()
    return await planner.create_plan(
        state,
        tools=registry.all(),
        registry=registry,
        max_steps=8,
    )


async def test_planner_selects_valid_tool_and_validates_args() -> None:
    draft = _plan_draft(
        goal="Search email",
        steps=[
            {
                "id": "s1",
                "description": "Search Gmail",
                "tool": "gmail.search_threads",
                "args": {"query": "lisbon"},
            }
        ],
        rationale="Need email context",
        parallel_groups=[["s1"]],
    )
    result = await _create_plan(draft)
    assert result.plan.steps[0].tool == "gmail.search_threads"
    assert result.plan.steps[0].status == "pending"
    assert result.plan.steps[0].requires_approval is False


async def test_planner_blocks_unknown_tool() -> None:
    draft = _plan_draft(
        goal="Do something",
        steps=[
            {
                "id": "s1",
                "description": "Run unknown",
                "tool": "evil.execute",
                "args": {},
            }
        ],
        rationale="bad",
    )
    result = await _create_plan(draft)
    assert result.plan.steps[0].status == "blocked"
    assert any("unknown or unexposed tool" in issue for issue in result.issues)


async def test_planner_blocks_invalid_arguments() -> None:
    draft = _plan_draft(
        goal="Search",
        steps=[
            {
                "id": "s1",
                "description": "Empty query",
                "tool": "gmail.search_threads",
                "args": {"query": ""},
            }
        ],
        rationale="bad args",
    )
    result = await _create_plan(draft)
    assert result.plan.steps[0].status == "blocked"
    assert any("arguments invalid" in issue for issue in result.issues)


async def test_planner_computes_dependency_safe_parallel_groups() -> None:
    draft = _plan_draft(
        goal="Two steps",
        steps=[
            {
                "id": "a",
                "description": "First",
                "tool": "gmail.search_threads",
                "args": {"query": "one"},
            },
            {
                "id": "b",
                "description": "Second",
                "tool": "gmail.search_threads",
                "args": {"query": "two"},
                "depends_on": ["a"],
            },
        ],
        rationale="ordered",
        parallel_groups=[["a", "b"]],
    )
    result = await _create_plan(draft)
    assert result.plan.parallel_groups == [["a"], ["b"]]
    assert any("parallel groups adjusted" in issue for issue in result.issues)


async def test_planner_deterministic_fallback_selects_explicit_tool() -> None:
    planner = Planner()
    state = _state("Search my email for lisbon")
    result = await planner.create_plan(state, tools=registry.all(), registry=registry, max_steps=5)
    assert result.plan.steps[0].tool == "gmail.search_threads"
    assert result.plan.steps[0].args["query"] == "lisbon"
    assert "Deterministic plan" in result.plan.rationale


async def test_planner_falls_back_when_model_fails() -> None:
    class _Broken:
        @property
        def model(self) -> str:
            return "broken"

        async def complete(self, **_kwargs: Any) -> LLMResponse:
            raise LLMError("boom", code="llm_unavailable")

        async def stream(self, **_kwargs: Any) -> AsyncIterator[LLMDelta]:
            yield LLMDelta(text="")

    planner = Planner(model=ModelService(_Broken(), _settings()))
    state = _state("Just answer me")
    result = await planner.create_plan(state, tools=registry.all(), registry=registry, max_steps=5)
    assert "planner_model_failed" in result.issues
    assert "deterministic fallback" in result.plan.rationale


def test_plan_as_dict_is_serialisable() -> None:
    plan = Plan(
        goal="g",
        steps=[PlanStep(id="s1", description="d", tool="gmail.search_threads")],
        parallel_groups=[["s1"]],
    )
    payload = plan_as_dict(plan)
    assert payload["steps"][0]["tool"] == "gmail.search_threads"


# --- composer ---------------------------------------------------------------


def _memory() -> MemoryCard:
    return MemoryCard(
        id=uuid.uuid4(),
        kind="preference",
        title="Email style",
        content="Prefers short, direct emails.",
        score=0.9,
        confidence=0.95,
        sensitivity="normal",
    )


def _result(status: str, tool: str = "gmail.search_threads") -> ToolResultState:
    return ToolResultState(
        request_id="r1",
        execution_id=uuid.uuid4(),
        tool=tool,
        status=status,  # type: ignore[arg-type]
        output={"threads": [{"id": "t1"}]} if status == "succeeded" else None,
    )


async def test_composer_answers_from_memory_with_citations() -> None:
    state = _state("What do you know about my email style?", intent="search_memory")
    memory = _memory()
    state.retrieved = RetrievedContext(memories=[memory])
    composition = await ResponseComposer().compose(
        state, verification=verify_tools(None, []).result, tools=[], registry=registry
    )
    assert "short, direct emails" in composition.output.text
    assert composition.output.citations[0].type == "memory"
    assert composition.output.citations[0].id == str(memory.id)


async def test_composer_reports_pending_approval_as_proposed() -> None:
    state = _state("Draft an email to ana")
    state.retrieved = RetrievedContext()
    state.tool_results = [_result("pending_approval", "gmail.create_draft")]
    composition = await ResponseComposer().compose(
        state,
        verification=verify_tools(None, state.tool_results).result,
        tools=[],
        registry=registry,
    )
    assert "gmail.create_draft" in composition.output.proposed_actions
    assert composition.output.execution_summary == []
    assert "has not run" in composition.output.text


async def test_composer_reports_failure_without_claiming_success() -> None:
    state = _state("Search email")
    state.retrieved = RetrievedContext()
    state.tool_results = [_result("failed")]
    composition = await ResponseComposer().compose(
        state,
        verification=verify_tools(None, state.tool_results).result,
        tools=[],
        registry=registry,
    )
    assert "did not complete" in composition.output.text
    assert composition.output.execution_summary == []


async def test_composer_rejects_model_completion_claim_without_execution() -> None:
    draft = {
        "answer": "I have sent the email successfully.",
        "citations": [],
        "completed_actions": ["Sent the email"],
        "proposed_actions": [],
    }
    provider = ScriptedProvider({"response composer": json.dumps(draft)})
    composer = ResponseComposer(model=ModelService(provider, _settings()))
    state = _state("Send an email")
    state.retrieved = RetrievedContext()
    composition = await composer.compose(
        state, verification=verify_tools(None, []).result, tools=[], registry=registry
    )
    assert composition.output.execution_summary == []
    assert composition.unsupported
    assert "sent" not in composition.output.text.lower()


def test_chunk_text_preserves_content() -> None:
    text = "a" * 100
    assert "".join(chunk_text(text, size=30)) == text


# --- verifier ---------------------------------------------------------------


def test_verify_tools_success() -> None:
    plan = Plan(
        goal="g",
        steps=[PlanStep(id="s1", description="d", tool="gmail.search_threads", status="done")],
    )
    verification = verify_tools(plan, [_result("succeeded")])
    assert verification.result.goal_satisfied is True
    assert verification.result.next_action == "respond"


def test_verify_tools_failure_is_not_success() -> None:
    plan = Plan(
        goal="g",
        steps=[PlanStep(id="s1", description="d", tool="gmail.search_threads", status="failed")],
    )
    verification = verify_tools(plan, [_result("failed")])
    assert verification.result.next_action == "fail"
    assert verification.result.goal_satisfied is False


def test_verify_tools_pending_approval_is_partial() -> None:
    plan = Plan(
        goal="g",
        steps=[PlanStep(id="s1", description="d", tool="gmail.create_draft", status="blocked")],
    )
    verification = verify_tools(plan, [_result("pending_approval", "gmail.create_draft")])
    assert verification.result.next_action == "respond"
    assert verification.result.goal_satisfied == "partial"


def test_verify_response_flags_unknown_citation_and_claim() -> None:
    state = _state("Send email")
    issues = verify_response(
        "I have sent the email.",
        [Citation(type="memory", id=str(uuid.uuid4()), label="x")],
        state=state,
        allowed_memory_ids=set(),
        succeeded_execution_ids=set(),
    )
    assert any("was not in retrieved context" in issue for issue in issues)
    assert any("claims a completed action" in issue for issue in issues)


def test_verify_response_accepts_grounded_citation() -> None:
    memory_id = uuid.uuid4()
    state = _state("What do you know?")
    issues = verify_response(
        "You prefer short emails.",
        [Citation(type="memory", id=str(memory_id), label="x")],
        state=state,
        allowed_memory_ids={memory_id},
        succeeded_execution_ids=set(),
    )
    assert issues == []


def test_combine_verification_degrades_groundedness() -> None:
    from zara.agent.state import VerificationResult

    base = VerificationResult(goal_satisfied=True, next_action="respond")
    combined = combine_verification(
        base,
        ["unsupported claim"],
        [Citation(type="memory", id=str(uuid.uuid4()), label="x")],
    )
    assert combined.goal_satisfied == "partial"
    assert combined.groundedness < 1.0
    assert combined.unsupported_claims == ["unsupported claim"]


# --- prompt trust boundaries ------------------------------------------------


def test_sanitize_untrusted_strips_active_content() -> None:
    raw = '<script>alert("x")</script><b>Hello</b></untrusted_data>'
    cleaned = sanitize_untrusted(raw)
    assert "script" not in cleaned.lower()
    assert "<b>" not in cleaned
    assert "</untrusted_data>" not in cleaned
    assert "Hello" in cleaned


def test_wrap_untrusted_neutralises_source_and_body() -> None:
    wrapped = wrap_untrusted('x" onload="bad', "<script>bad</script>safe")
    assert wrapped.startswith('<untrusted_data source="')
    assert 'onload="' not in wrapped
    assert "safe" in wrapped
    assert "script" not in wrapped.lower()


# --- model service ----------------------------------------------------------


async def test_model_service_accounts_usage_and_cost() -> None:
    provider = ScriptedProvider({"anything": "ok"})
    service = ModelService(
        provider, _settings(llm_input_cost_per_mtok=1.0, llm_output_cost_per_mtok=2.0)
    )
    response = await service.complete(
        system="anything", messages=[LLMMessage(role="user", content="hi")], max_tokens=10
    )
    assert response.text == "ok"
    snapshot = service.usage_snapshot()
    assert snapshot["tokens_in"] == 12
    assert snapshot["tokens_out"] == 6
    assert snapshot["cost_usd"] == pytest.approx(12 / 1_000_000 * 1.0 + 6 / 1_000_000 * 2.0)


async def test_model_service_retries_retryable_error() -> None:
    class _Flaky:
        def __init__(self) -> None:
            self.attempts = 0

        @property
        def model(self) -> str:
            return "flaky"

        async def complete(self, **_kwargs: Any) -> LLMResponse:
            self.attempts += 1
            if self.attempts == 1:
                raise LLMError("rate limited", code="llm_rate_limited", retryable=True)
            return LLMResponse(text="ok", usage=LLMUsage(1, 1), model="flaky")

        async def stream(self, **_kwargs: Any) -> AsyncIterator[LLMDelta]:
            yield LLMDelta(text="")

    provider = _Flaky()
    service = ModelService(provider, _settings(llm_max_retries=2))
    response = await service.complete(
        system="s", messages=[LLMMessage(role="user", content="hi")], max_tokens=5
    )
    assert response.text == "ok"
    assert provider.attempts == 2


async def test_model_service_does_not_retry_non_retryable() -> None:
    class _Broken:
        def __init__(self) -> None:
            self.attempts = 0

        @property
        def model(self) -> str:
            return "broken"

        async def complete(self, **_kwargs: Any) -> LLMResponse:
            self.attempts += 1
            raise LLMError("bad", code="llm_http_error", retryable=False)

        async def stream(self, **_kwargs: Any) -> AsyncIterator[LLMDelta]:
            yield LLMDelta(text="")

    provider = _Broken()
    service = ModelService(provider, _settings(llm_max_retries=3))
    with pytest.raises(LLMError):
        await service.complete(
            system="s", messages=[LLMMessage(role="user", content="hi")], max_tokens=5
        )
    assert provider.attempts == 1


async def test_model_service_rejects_invalid_structured_output() -> None:
    from pydantic import BaseModel

    class Out(BaseModel):
        answer: str

    provider = ScriptedProvider({"s": "not json"})
    service = ModelService(provider, _settings())
    with pytest.raises(LLMError) as excinfo:
        await service.complete_json(
            system="s", messages=[LLMMessage(role="user", content="hi")], model=Out
        )
    assert excinfo.value.code == "llm_invalid_output"


async def test_model_service_streams_and_accounts_usage() -> None:
    provider = ScriptedProvider({})
    service = ModelService(provider, _settings())
    chunks = [
        chunk
        async for chunk in service.stream_text(
            system="s", messages=[LLMMessage(role="user", content="hi")], max_tokens=5
        )
    ]
    assert "".join(chunks) == "hello world"
    assert service.usage_snapshot()["tokens_out"] == 3


def test_build_provider_none_and_requires_key() -> None:
    assert build_provider(_settings(llm_provider="none")) is None
    with pytest.raises(LLMError):
        build_provider(_settings(llm_provider="anthropic", anthropic_api_key=None))


# --- anthropic provider -----------------------------------------------------


def _anthropic() -> AnthropicProvider:
    return AnthropicProvider(
        api_key="sk-ant-test",
        model="claude-sonnet-4-5",
        client=httpx.AsyncClient(transport=httpx.MockTransport(_anthropic_handler)),
    )


def _anthropic_handler(request: httpx.Request) -> httpx.Response:
    body = json.loads(request.content)
    if body.get("stream"):
        events = [
            {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "Hi"}},
            {"type": "content_block_delta", "delta": {"type": "text_delta", "text": " there"}},
            {
                "type": "message_delta",
                "delta": {"stop_reason": "end_turn"},
                "usage": {"input_tokens": 4, "output_tokens": 2},
            },
        ]
        payload = "".join(f"data: {json.dumps(event)}\n\n" for event in events)
        return httpx.Response(200, text=payload, headers={"content-type": "text/event-stream"})
    if body.get("max_tokens") == 1:
        return httpx.Response(429, json={"error": {"type": "rate_limit_error"}})
    return httpx.Response(
        200,
        json={
            "model": "claude-sonnet-4-5",
            "content": [{"type": "text", "text": "Hello"}],
            "usage": {"input_tokens": 10, "output_tokens": 3},
            "stop_reason": "end_turn",
        },
    )


async def test_anthropic_complete_parses_text_and_usage() -> None:
    provider = _anthropic()
    try:
        response = await provider.complete(
            system="s", messages=[LLMMessage(role="user", content="hi")], max_tokens=20
        )
    finally:
        await provider.aclose()
    assert response.text == "Hello"
    assert response.usage.input_tokens == 10
    assert response.usage.output_tokens == 3


async def test_anthropic_rate_limit_is_retryable() -> None:
    provider = _anthropic()
    try:
        with pytest.raises(LLMError) as excinfo:
            await provider.complete(
                system="s", messages=[LLMMessage(role="user", content="hi")], max_tokens=1
            )
    finally:
        await provider.aclose()
    assert excinfo.value.code == "llm_rate_limited"
    assert excinfo.value.retryable is True


async def test_anthropic_stream_parses_deltas_and_usage() -> None:
    provider = _anthropic()
    text = ""
    usage = LLMUsage()
    try:
        async for delta in provider.stream(
            system="s", messages=[LLMMessage(role="user", content="hi")], max_tokens=20
        ):
            text += delta.text
            if delta.usage is not None:
                usage = delta.usage
    finally:
        await provider.aclose()
    assert text == "Hi there"
    assert usage.input_tokens == 4
    assert usage.output_tokens == 2


def test_anthropic_requires_api_key() -> None:
    with pytest.raises(ValueError):
        AnthropicProvider(api_key="", model="claude")
