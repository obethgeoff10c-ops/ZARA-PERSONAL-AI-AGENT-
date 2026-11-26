"""Integration tests for the tool execution pipeline (real DB, mocked provider)."""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from zara.auth.principal import Principal
from zara.config import Settings, get_settings
from zara.db.engine import create_engine
from zara.errors import AppError
from zara.models import Approval, AuditLog, Integration, ToolExecution
from zara.security import credential_aad, encrypt_many
from zara.tools.builtin.google import CreateDraftIn, SearchThreadsIn
from zara.tools.executor import (
    ExecutionContext,
    approve,
    canonical_args,
    execute,
    execute_approved,
)

from helpers import Api, unique_email, unique_org

pytestmark = pytest.mark.integration

_ACCESS = "ya29.executor-access-token"
_SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.compose",
]


def _settings() -> Settings:
    return get_settings().model_copy(
        update={
            "redis_url": None,
            "google_oauth_client_id": "google-client-id",
            "google_oauth_client_secret": SecretStr("google-client-secret"),
        }
    )


@asynccontextmanager
async def _db(settings: Settings, principal: Principal) -> AsyncIterator[AsyncSession]:
    """Dedicated engine/session so tests never share a pool across event loops."""
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


def _principal(tenant_id: uuid.UUID, user_id: uuid.UUID, email: str) -> Principal:
    return Principal(
        tenant_id=tenant_id,
        user_id=user_id,
        role="member",
        email=email,
        display_name=None,
        auth_provider="dev",
        external_auth_id="dev",
    )


async def _seed_integration(
    api: Api,
    token: str,
    *,
    scopes: list[str] | None = None,
    refresh_token: str | None = None,
    expires_in: int = 3600,
) -> tuple[Principal, uuid.UUID]:
    me = api.get("/me", token=token).json()
    principal = _principal(uuid.UUID(me["tenant_id"]), uuid.UUID(me["id"]), me["email"])
    settings = _settings()
    integration_id = uuid.uuid4()
    aad = credential_aad(principal.tenant_id, principal.user_id, integration_id)
    plaintexts = {"access": _ACCESS.encode()}
    if refresh_token:
        plaintexts["refresh"] = refresh_token.encode()
    sealed = encrypt_many(settings, plaintexts=plaintexts, aad=aad)

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
                    "scopes": scopes or _SCOPES,
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
                            :refresh_ct, :expires, :dek, :kms, 1, now())
                    """
                ),
                {
                    "id": uuid.uuid4(),
                    "tenant": principal.tenant_id,
                    "integration": integration_id,
                    "ct": sealed.ciphertexts["access"],
                    "refresh_ct": sealed.ciphertexts.get("refresh"),
                    "expires": datetime.now(UTC) + timedelta(seconds=expires_in),
                    "dek": sealed.dek_wrapped,
                    "kms": sealed.kms_key_id,
                },
            )
    finally:
        await engine.dispose()
    return principal, integration_id


def _gmail_handler(calls: list[httpx.Request]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.method == "POST" and request.url.path.endswith("/drafts"):
            return httpx.Response(200, json={"id": "draft-1"})
        if request.url.path.endswith("/threads"):
            return httpx.Response(200, json={"threads": [{"id": "t1"}]})
        if request.url.path.endswith("/threads/t1"):
            return httpx.Response(
                200,
                json={
                    "id": "t1",
                    "messages": [
                        {
                            "id": "m1",
                            "internalDate": "1735689600000",
                            "snippet": "Budget",
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


async def test_read_tool_executes_once_and_is_audited(api: Api) -> None:
    token = api.dev_token(unique_email("tool"), org_id=unique_org())
    principal, _ = await _seed_integration(api, token)
    settings = _settings()
    calls: list[httpx.Request] = []
    http = httpx.AsyncClient(transport=_gmail_handler(calls))
    step_id = uuid.uuid4()
    ctx = ExecutionContext(
        principal=principal,
        settings=settings,
        http=http,
        autonomy="draft",
        step_id=step_id,
    )

    try:
        async with _db(settings, principal) as db:
            first = await execute(
                db,
                principal,
                settings,
                ctx,
                tool_name="gmail.search_threads",
                raw_args=SearchThreadsIn(query="lisbon").model_dump(),
            )
            assert first.status == "succeeded"
            assert first.output is not None
            assert len(calls) == 2

        async with _db(settings, principal) as db:
            replay = await execute(
                db,
                principal,
                settings,
                ctx,
                tool_name="gmail.search_threads",
                raw_args=SearchThreadsIn(query="lisbon").model_dump(),
            )
            assert replay.status == "succeeded"
            assert replay.replayed is True
            executions = (
                (
                    await db.execute(
                        select(ToolExecution).where(
                            ToolExecution.run_id.is_(None),
                            ToolExecution.tool_name == "gmail.search_threads",
                        )
                    )
                )
                .scalars()
                .all()
            )
            assert len(executions) == 1
            assert executions[0].status == "succeeded"
            assert executions[0].result_summary is not None
            assert executions[0].result_summary["threads_count"] == 1
            audit = (
                await db.execute(
                    select(AuditLog).where(
                        AuditLog.action == "tool.executed",
                        AuditLog.resource_id == str(executions[0].id),
                    )
                )
            ).scalar_one_or_none()
            assert audit is not None
        assert len(calls) == 2
    finally:
        await http.aclose()


async def test_denied_scope_and_kill_switch_do_not_call_provider(api: Api) -> None:
    token = api.dev_token(unique_email("tool"), org_id=unique_org())
    principal, _ = await _seed_integration(
        api, token, scopes=["https://www.googleapis.com/auth/gmail.compose"]
    )
    settings = _settings()
    calls: list[httpx.Request] = []
    http = httpx.AsyncClient(transport=_gmail_handler(calls))

    try:
        async with _db(settings, principal) as db:
            denied = await execute(
                db,
                principal,
                settings,
                ExecutionContext(principal=principal, settings=settings, http=http),
                tool_name="gmail.search_threads",
                raw_args={"query": "lisbon"},
            )
            assert denied.status == "denied"
            assert denied.reason == "scope_missing"

            killed = await execute(
                db,
                principal,
                settings,
                ExecutionContext(
                    principal=principal,
                    settings=settings,
                    http=http,
                    kill_switches=frozenset({"kill.tool.gmail.read_thread"}),
                ),
                tool_name="gmail.read_thread",
                raw_args={"thread_id": "t1"},
            )
            assert killed.status == "denied"
            assert killed.reason.startswith("kill_switch")
        assert calls == []
    finally:
        await http.aclose()


async def test_approval_hash_binding_and_approved_execution(api: Api) -> None:
    token = api.dev_token(unique_email("tool"), org_id=unique_org())
    principal, _ = await _seed_integration(api, token)
    settings = _settings()
    calls: list[httpx.Request] = []
    http = httpx.AsyncClient(transport=_gmail_handler(calls))
    draft_args = CreateDraftIn(
        to=["ana@example.com"],
        subject="Trip plan",
        body_text="Here is the plan.",
    )
    ctx = ExecutionContext(
        principal=principal,
        settings=settings,
        http=http,
        autonomy="suggest",
        step_id=uuid.uuid4(),
    )

    try:
        async with _db(settings, principal) as db:
            pending = await execute(
                db,
                principal,
                settings,
                ctx,
                tool_name="gmail.create_draft",
                raw_args=draft_args.model_dump(),
            )
            assert pending.status == "pending_approval"
            assert pending.approval_id is not None
            assert calls == []

            approval = (
                await db.execute(select(Approval).where(Approval.id == pending.approval_id))
            ).scalar_one()
            assert approval.preview["title"] == "Create Gmail draft"
            assert approval.preview["fields"]["To"] == "ana@example.com"
            assert approval.args_hash == hashlib.sha256(canonical_args(draft_args)).digest()

            with pytest.raises(AppError) as mismatch:
                await approve(
                    db,
                    principal,
                    approval.id,
                    args_hash=hashlib.sha256(b"other args").digest(),
                )
            assert mismatch.value.code == "approval_args_mismatch"

            approved = await approve(db, principal, approval.id, args_hash=approval.args_hash)
            assert approved.status == "approved"

            result = await execute_approved(db, principal, settings, ctx, approval.id)
            assert result.status == "succeeded"
            assert result.output is not None
            assert result.output.model_dump()["draft_id"] == "draft-1"
            assert len(calls) == 1

            replay = await execute_approved(db, principal, settings, ctx, approval.id)
            assert replay.replayed is True
            assert len(calls) == 1

            execution = (
                await db.execute(
                    select(ToolExecution).where(ToolExecution.id == pending.execution_id)
                )
            ).scalar_one()
            assert execution.status == "succeeded"
            assert execution.approval_id == approval.id
            assert execution.args["body_text"] == "Here is the plan."
    finally:
        await http.aclose()


async def test_edited_approval_rehashes_and_executes_new_args(api: Api) -> None:
    token = api.dev_token(unique_email("tool"), org_id=unique_org())
    principal, _ = await _seed_integration(api, token)
    settings = _settings()
    calls: list[httpx.Request] = []
    http = httpx.AsyncClient(transport=_gmail_handler(calls))
    ctx = ExecutionContext(
        principal=principal,
        settings=settings,
        http=http,
        autonomy="suggest",
        step_id=uuid.uuid4(),
    )

    try:
        async with _db(settings, principal) as db:
            pending = await execute(
                db,
                principal,
                settings,
                ctx,
                tool_name="gmail.create_draft",
                raw_args=CreateDraftIn(
                    to=["ana@example.com"], subject="Original", body_text="Original body"
                ).model_dump(),
            )
            edited = CreateDraftIn(
                to=["ana@example.com"], subject="Edited", body_text="Edited body"
            )
            approval = await approve(
                db, principal, pending.approval_id, edited_args=edited.model_dump(mode="json")
            )
            assert approval.status == "edited_approved"
            assert approval.preview["fields"]["Subject"] == "Edited"

            result = await execute_approved(db, principal, settings, ctx, approval.id)
            assert result.status == "succeeded"
            execution = (
                await db.execute(
                    select(ToolExecution).where(ToolExecution.id == pending.execution_id)
                )
            ).scalar_one()
            assert execution.args["subject"] == "Edited"
            assert execution.args_hash == approval.edited_args_hash
    finally:
        await http.aclose()


async def test_failed_refresh_marks_needs_reauth_and_denies(api: Api) -> None:
    token = api.dev_token(unique_email("tool"), org_id=unique_org())
    principal, integration_id = await _seed_integration(
        api, token, refresh_token="refresh-value", expires_in=-10
    )
    settings = _settings()
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.url.path == "/token":
            return httpx.Response(400, json={"error": "invalid_grant"})
        return httpx.Response(500)

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        async with _db(settings, principal) as db:
            with pytest.raises(AppError) as excinfo:
                await execute(
                    db,
                    principal,
                    settings,
                    ExecutionContext(principal=principal, settings=settings, http=http),
                    tool_name="gmail.search_threads",
                    raw_args={"query": "lisbon"},
                )
            assert excinfo.value.code == "needs_reauth"
            integration = (
                await db.execute(select(Integration).where(Integration.id == integration_id))
            ).scalar_one()
            assert integration.status == "needs_reauth"
            assert integration.last_error == "invalid_grant"
        assert [request.url.path for request in calls] == ["/token"]
    finally:
        await http.aclose()


async def test_cross_user_integrations_are_never_reused(api: Api) -> None:
    owner = api.dev_token(unique_email("tool"), org_id=unique_org())
    other = api.dev_token(unique_email("tool"), org_id=unique_org())
    owner_principal, _ = await _seed_integration(api, owner)
    me = api.get("/me", token=other).json()
    other_principal = _principal(uuid.UUID(me["tenant_id"]), uuid.UUID(me["id"]), me["email"])
    settings = _settings()
    calls: list[httpx.Request] = []
    http = httpx.AsyncClient(transport=_gmail_handler(calls))

    try:
        async with _db(settings, other_principal) as db:
            result = await execute(
                db,
                other_principal,
                settings,
                ExecutionContext(principal=other_principal, settings=settings, http=http),
                tool_name="gmail.search_threads",
                raw_args={"query": "lisbon"},
            )
            assert result.status == "denied"
            assert result.reason == "needs_reauth"
            integrations = (await db.execute(select(Integration))).scalars().all()
            assert integrations == []
        assert calls == []
    finally:
        await http.aclose()

    assert owner_principal.tenant_id != other_principal.tenant_id
