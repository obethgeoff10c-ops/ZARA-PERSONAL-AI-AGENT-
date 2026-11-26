"""Integration tests for the Prompt 06 agent engine (real DB, mocked provider)."""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from zara.agent.engine import AgentEngine
from zara.agent.planner import PlanBuildResult, Planner
from zara.agent.state import (
    AgentState,
    Plan,
    PlanStep,
    RunStatus,
)
from zara.auth.principal import Principal
from zara.config import Settings, get_settings
from zara.db.engine import create_engine
from zara.llm.base import LLMDelta, LLMMessage, LLMResponse, LLMUsage
from zara.models import Approval, Message, RunEvent, ToolExecution
from zara.security import credential_aad, encrypt_many
from zara.services import conversations, runs
from zara.services.memory.crud import create_memory
from zara.tools.builtin.google import CreateDraftIn

from helpers import Api, unique_email, unique_org

pytestmark = pytest.mark.integration

_ACCESS = "ya29.agent-access-token"
_SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.compose",
]


def _settings() -> Settings:
    return get_settings().model_copy(
        update={
            "redis_url": None,
            "llm_provider": "none",
            "google_oauth_client_id": "google-client-id",
            "google_oauth_client_secret": SecretStr("google-client-secret"),
        }
    )


@asynccontextmanager
async def _db(settings: Settings, principal: Principal) -> AsyncIterator[AsyncSession]:
    engine = create_engine(settings)
    maker = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    try:
        async with maker() as session:
            await session.execute(text("SET LOCAL ROLE zara_app"))
            await session.execute(
                text(
                    "SELECT set_config('app.tenant_id', :tenant, true),"
                    " set_config('app.user_id', :user, true),"
                    " set_config('app.role', 'user', true)"
                ),
                {"tenant": str(principal.tenant_id), "user": str(principal.user_id)},
            )
            try:
                yield session
                await session.commit()
            except BaseException:
                await session.rollback()
                raise
    finally:
        await engine.dispose()


def _principal_from_api(api: Api, token: str) -> Principal:
    me = api.get("/me", token=token).json()
    return Principal(
        tenant_id=uuid.UUID(me["tenant_id"]),
        user_id=uuid.UUID(me["id"]),
        role="member",
        email=me["email"],
        display_name=None,
        auth_provider="dev",
        external_auth_id="dev",
    )


async def _seed_gmail(settings: Settings, principal: Principal) -> uuid.UUID:
    integration_id = uuid.uuid4()
    aad = credential_aad(principal.tenant_id, principal.user_id, integration_id)
    sealed = encrypt_many(settings, plaintexts={"access": _ACCESS.encode()}, aad=aad)
    engine = create_engine(settings)
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    """
                    INSERT INTO integrations.integrations
                        (id, tenant_id, user_id, provider, account_identifier, status,
                         granted_scopes, connected_at)
                    VALUES (:id, :tenant, :user, 'gmail', 'ana@example.com', 'active',
                            :scopes, now())
                    """
                ),
                {
                    "id": integration_id,
                    "tenant": principal.tenant_id,
                    "user": principal.user_id,
                    "scopes": _SCOPES,
                },
            )
            await connection.execute(
                text(
                    """
                    INSERT INTO integrations.oauth_credentials
                        (id, tenant_id, integration_id, token_type, access_token_ct,
                         refresh_token_ct, access_expires_at, dek_wrapped,
                         kms_key_id, enc_version, last_refreshed_at)
                    VALUES (:id, :tenant, :integration, 'Bearer', :ct,
                            NULL, :expires, :dek, :kms, 1, now())
                    """
                ),
                {
                    "id": uuid.uuid4(),
                    "tenant": principal.tenant_id,
                    "integration": integration_id,
                    "ct": sealed.ciphertexts["access"],
                    "expires": datetime.now(UTC) + timedelta(seconds=3600),
                    "dek": sealed.dek_wrapped,
                    "kms": sealed.kms_key_id,
                },
            )
    finally:
        await engine.dispose()
    return integration_id


def _gmail_ok(calls: list[httpx.Request]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.url.path.endswith("/threads"):
            return httpx.Response(
                200,
                json={
                    "threads": [
                        {"id": "t1", "snippet": "Lisbon budget", "internalDate": "1735689600000"}
                    ]
                },
            )
        if request.url.path.endswith("/threads/t1"):
            return httpx.Response(
                200,
                json={
                    "id": "t1",
                    "messages": [
                        {
                            "id": "m1",
                            "internalDate": "1735689600000",
                            "snippet": "Lisbon budget",
                            "payload": {
                                "headers": [
                                    {"name": "Subject", "value": "Lisbon budget"},
                                    {"name": "From", "value": "ana@example.com"},
                                    {"name": "To", "value": "me@example.com"},
                                ]
                            },
                        }
                    ],
                },
            )
        return httpx.Response(404)

    return httpx.MockTransport(handler)


def _gmail_failing(calls: list[httpx.Request]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(500, json={"error": "boom"})

    return httpx.MockTransport(handler)


class _FixedPlanner(Planner):
    """Planner double that returns a pre-built plan (still permission-checked)."""

    def __init__(self, steps: list[PlanStep], goal: str) -> None:
        super().__init__()
        self._steps = steps
        self._goal = goal

    async def create_plan(self, state: AgentState, **kwargs: Any) -> PlanBuildResult:
        _ = kwargs
        return PlanBuildResult(
            plan=Plan(goal=self._goal, steps=self._steps, rationale="fixed test plan")
        )


class _ScriptedProvider:
    def __init__(self, responses: dict[str, str]) -> None:
        self._responses = responses
        self.calls: list[str] = []

    @property
    def model(self) -> str:
        return "scripted"

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
        for marker, payload in self._responses.items():
            if marker in system:
                return LLMResponse(text=payload, usage=LLMUsage(10, 5), model="scripted")
        return LLMResponse(text="{}", usage=LLMUsage(1, 1), model="scripted")

    async def stream(
        self,
        *,
        system: str,
        messages: Sequence[LLMMessage],
        max_tokens: int,
        temperature: float = 0.0,
    ) -> AsyncIterator[LLMDelta]:
        yield LLMDelta(text="")


async def _run(
    engine: AgentEngine,
    settings: Settings,
    principal: Principal,
    *,
    goal: str,
    conversation_id: uuid.UUID | None = None,
) -> tuple[AgentState, uuid.UUID, list[RunEvent]]:
    events: list[RunEvent] = []

    async def sink(event: RunEvent) -> None:
        events.append(event)

    async with _db(settings, principal) as db:
        run = await runs.create_run(
            db,
            principal,
            goal=goal,
            domains=None,
            conversation_id=conversation_id,
            mode="auto",
            budget=None,
        )
        state = await engine.run(db, principal, settings, run.id, on_event=sink)
        run_id = run.id
    return state, run_id, events


# --- acceptance -------------------------------------------------------------


async def test_agent_answers_from_stored_memory_with_summary(api: Api) -> None:
    token = api.dev_token(unique_email("agent"), org_id=unique_org())
    principal = _principal_from_api(api, token)
    settings = _settings()

    async with _db(settings, principal) as db:
        conversation = await conversations.create_conversation(db, principal, title="Style")
        memory = await create_memory(
            db,
            principal,
            {
                "kind": "preference",
                "title": "Email style",
                "content": "I prefer short, direct emails with one clear ask.",
                "importance": 0.9,
                "confidence": 0.95,
            },
        )
        message = await conversations.append_user_message(
            db,
            principal,
            conversation.id,
            content="What do you know about my email style?",
        )
        run = await runs.create_run(
            db,
            principal,
            goal=message.content,
            domains=None,
            conversation_id=conversation.id,
            mode="auto",
            budget=None,
        )
        run.trigger_ref = {"user_message_id": str(message.id)}
        await db.flush()

        state = await AgentEngine(provider=None).run(db, principal, settings, run.id)
        conversation_id = conversation.id
        memory_id = memory.id
        run_id = run.id

    assert state.status is RunStatus.SUCCEEDED
    assert state.final_output is not None
    assert "short, direct emails" in state.final_output.text
    assert any(
        citation.type == "memory" and citation.id == str(memory_id)
        for citation in state.final_output.citations
    )
    assert state.plan is not None
    assert state.verification is not None

    async with _db(settings, principal) as db:
        run_row = await runs.get_run(db, principal, run_id)
        assert run_row.status == "succeeded"
        assert run_row.state_snapshot is not None
        assert run_row.usage is not None
        events = (
            await db.execute(
                select(RunEvent).where(RunEvent.run_id == run_id).order_by(RunEvent.seq.asc())
            )
        ).scalars()
        types = [event.type for event in events]
        assert "run.started" in types
        assert "route.decided" in types
        assert "memory.retrieved" in types
        assert "plan.created" in types
        assert "message.completed" in types
        assert types[-1] == "run.status"

        messages = (
            await db.execute(
                select(Message)
                .where(Message.conversation_id == conversation_id)
                .order_by(Message.created_at.asc())
            )
        ).scalars()
        assistant = [m for m in messages if m.role == "assistant"]
        assert len(assistant) == 1
        assert "short, direct emails" in assistant[0].content


# --- tool execution and verification ---------------------------------------


async def test_agent_executes_read_tool_and_verifies(api: Api) -> None:
    token = api.dev_token(unique_email("agent"), org_id=unique_org())
    principal = _principal_from_api(api, token)
    settings = _settings()
    await _seed_gmail(settings, principal)
    calls: list[httpx.Request] = []
    http = httpx.AsyncClient(transport=_gmail_ok(calls))
    try:
        state, run_id, events = await _run(
            AgentEngine(provider=None, http_client=http),
            settings,
            principal,
            goal="Search my email for lisbon",
        )
    finally:
        await http.aclose()

    assert state.status is RunStatus.SUCCEEDED
    assert state.final_output is not None
    assert len(state.tool_results) == 1
    assert state.tool_results[0].status == "succeeded"
    assert state.tool_results[0].tool == "gmail.search_threads"
    assert state.verification is not None
    assert state.verification.goal_satisfied is True
    summary = state.final_output.execution_summary
    assert "gmail.search_threads completed and was verified." in summary
    assert any(citation.type == "tool_execution" for citation in state.final_output.citations)
    event_types = [event.type for event in events]
    assert "tool.started" in event_types
    assert "tool.finished" in event_types
    assert calls

    async with _db(settings, principal) as db:
        execution = (
            await db.execute(select(ToolExecution).where(ToolExecution.run_id == run_id))
        ).scalar_one()
        assert execution.status == "succeeded"


async def test_agent_reports_tool_failure_instead_of_success(api: Api) -> None:
    token = api.dev_token(unique_email("agent"), org_id=unique_org())
    principal = _principal_from_api(api, token)
    settings = _settings()
    await _seed_gmail(settings, principal)
    calls: list[httpx.Request] = []
    http = httpx.AsyncClient(transport=_gmail_failing(calls))
    try:
        state, run_id, _ = await _run(
            AgentEngine(provider=None, http_client=http),
            settings,
            principal,
            goal="Search my email for lisbon",
        )
    finally:
        await http.aclose()

    assert state.status is RunStatus.FAILED
    assert state.final_output is not None
    assert state.final_output.execution_summary == []
    assert state.verification is not None
    assert state.verification.goal_satisfied is False
    lowered = state.final_output.text.lower()
    assert "did not complete" in lowered or "not claiming" in lowered
    assert "failed and was not reported as completed" in lowered

    async with _db(settings, principal) as db:
        run_row = await runs.get_run(db, principal, run_id)
        assert run_row.status == "failed"
        assert run_row.error is not None


# --- approval boundary ------------------------------------------------------


async def test_write_tool_waits_for_approval_and_does_not_run(api: Api) -> None:
    token = api.dev_token(unique_email("agent"), org_id=unique_org())
    principal = _principal_from_api(api, token)
    settings = _settings()
    await _seed_gmail(settings, principal)
    calls: list[httpx.Request] = []
    http = httpx.AsyncClient(transport=_gmail_ok(calls))
    planner = _FixedPlanner(
        steps=[
            PlanStep(
                id="s1",
                description="Draft reply",
                tool="gmail.create_draft",
                args=CreateDraftIn(
                    to=["ana@example.com"],
                    subject="Lisbon",
                    body_text="Here is the budget.",
                ).model_dump(),
                risk_tier=3,
                requires_approval=True,
            )
        ],
        goal="Draft a reply to ana",
    )
    try:
        state, run_id, events = await _run(
            AgentEngine(provider=None, planner=planner, http_client=http),
            settings,
            principal,
            goal="Draft a reply to ana",
        )
    finally:
        await http.aclose()

    assert state.status is RunStatus.AWAITING_APPROVAL
    assert calls == []  # nothing was sent to Gmail
    assert state.final_output is not None
    assert "gmail.create_draft" in state.final_output.proposed_actions
    assert state.final_output.execution_summary == []
    assert "has not run" in state.final_output.text
    assert any(event.type == "approval.requested" for event in events)

    async with _db(settings, principal) as db:
        run_row = await runs.get_run(db, principal, run_id)
        assert run_row.status == "awaiting_approval"
        approval = (
            await db.execute(select(Approval).where(Approval.run_id == run_id))
        ).scalar_one()
        assert approval.status == "pending"
        execution = (
            await db.execute(select(ToolExecution).where(ToolExecution.run_id == run_id))
        ).scalar_one()
        assert execution.status == "pending"


# --- hallucination prevention ----------------------------------------------


async def test_hallucinated_completion_is_replaced_with_grounded_answer(api: Api) -> None:
    token = api.dev_token(unique_email("agent"), org_id=unique_org())
    principal = _principal_from_api(api, token)
    settings = _settings()
    provider = _ScriptedProvider(
        {
            "context router": json.dumps(
                {
                    "domains": [{"domain": "PERSONAL", "score": 0.8, "reasons": ["personal"]}],
                    "intent": {
                        "label": "answer_question",
                        "confidence": 0.9,
                        "needs_plan": False,
                        "write_likely": False,
                    },
                    "required_integrations": [],
                    "entity_hints": [],
                }
            ),
            "planner": json.dumps(
                {
                    "goal": "Answer the user",
                    "steps": [{"id": "s1", "description": "Answer", "tool": None, "args": {}}],
                    "rationale": "no tools",
                    "parallel_groups": [["s1"]],
                }
            ),
            "response composer": json.dumps(
                {
                    "answer": "I have sent the email successfully.",
                    "citations": [],
                    "completed_actions": ["Sent the email"],
                    "proposed_actions": [],
                }
            ),
        }
    )
    state, _, _ = await _run(
        AgentEngine(provider=provider),
        settings,
        principal,
        goal="Did you send the email?",
    )

    assert state.final_output is not None
    lowered = state.final_output.text.lower()
    assert "sent the email successfully" not in lowered
    assert "i have sent" not in lowered
    assert state.final_output.execution_summary == []
    assert state.verification is not None
    assert state.verification.unsupported_claims


# --- streaming transport ----------------------------------------------------


def _parse_sse(raw: str) -> list[tuple[str, dict[str, Any]]]:
    parsed: list[tuple[str, dict[str, Any]]] = []
    for block in raw.split("\n\n"):
        event_name = None
        data: dict[str, Any] | None = None
        for line in block.splitlines():
            if line.startswith("event:"):
                event_name = line.removeprefix("event:").strip()
            elif line.startswith("data:"):
                data = json.loads(line.removeprefix("data:").strip())
        if event_name is not None and data is not None:
            parsed.append((event_name, data))
    return parsed


def test_chat_streams_persisted_events_and_message(api: Api) -> None:
    token = api.dev_token(unique_email("chat"), org_id=unique_org())
    api.client.app.state.agent_engine = AgentEngine(provider=None)

    with api.client.stream(
        "POST",
        "/api/v1/chat",
        headers=api.headers(token),
        json={"message": "Hello there"},
    ) as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        raw = "".join(response.iter_text())

    events = _parse_sse(raw)
    names = [name for name, _ in events]
    assert "run.started" in names
    assert "route.decided" in names
    assert "memory.retrieved" in names
    assert "plan.created" in names
    assert "token" in names
    assert "message.completed" in names
    assert "run.status" in names
    assert names[-1] == "done"

    run_started = next(data for name, data in events if name == "run.started")
    conversation_id = run_started["conversation_id"]
    assert conversation_id

    messages = api.get(f"/conversations/{conversation_id}/messages", token=token).json()
    assert any(item["role"] == "assistant" for item in messages["items"])
