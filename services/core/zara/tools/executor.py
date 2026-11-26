"""Tool execution pipeline (05-TOOLS.md §5).

Responsibilities: strict input validation, idempotency, policy enforcement,
provider credential loading/refresh, one audited execution row per call, and
approval creation with a hash bound to the exact args.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from pydantic import BaseModel, ValidationError
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from zara.auth.principal import Principal
from zara.config import Settings
from zara.errors import AppError, conflict, not_found, validation_failed
from zara.models import Approval, Integration, ToolExecution
from zara.providers import github as github_provider
from zara.providers import google as google_provider
from zara.services import oauth
from zara.services.audit import record_event
from zara.tools import registry
from zara.tools.policy import PolicyContext, PolicyDecision, decide
from zara.tools.preview import render as render_preview
from zara.tools.registry import ToolRegistry
from zara.tools.spec import ToolSpec

_READ_TIMEOUT = 15


@dataclass(slots=True)
class ExecutionContext:
    principal: Principal
    settings: Settings
    http: httpx.AsyncClient
    autonomy: str = "suggest"
    domains: frozenset[str] = frozenset({"gmail", "gcal", "gdrive", "github"})
    run_id: uuid.UUID | None = None
    step_id: uuid.UUID | None = None
    run_tainted: bool = False
    critical_arg_untrusted: bool = False
    anomaly: bool = False
    budget_exceeded: bool = False
    kill_switches: frozenset[str] = field(default_factory=frozenset)
    idempotency_key: str | None = None
    preview_renderer: Callable[[str, dict[str, Any]], dict[str, Any]] = render_preview


@dataclass(slots=True)
class ToolResult:
    status: str
    execution_id: uuid.UUID
    output: BaseModel | None = None
    approval_id: uuid.UUID | None = None
    reason: str | None = None
    replayed: bool = False


Handler = Callable[[httpx.AsyncClient, str, Any], Awaitable[BaseModel]]

_HANDLERS: dict[str, Handler] = {
    "gmail.search_threads": google_provider.search_threads,
    "gmail.read_thread": google_provider.read_thread,
    "gmail.create_draft": google_provider.create_draft,
    "gcal.list_events": google_provider.list_events,
    "gcal.create_hold": google_provider.create_hold,
    "gcal.create_event": google_provider.create_event,
    "gcal.respond_to_invite": google_provider.respond_to_invite,
    "gcal.delete_event": google_provider.delete_event,
    "drive.search_files": google_provider.search_files,
    "drive.read_file": google_provider.read_file,
    "drive.create_doc_draft": google_provider.create_doc_draft,
    "drive.share": google_provider.share_file,
    "github.list_repos": github_provider.list_repos,
    "github.list_pull_requests": github_provider.list_pull_requests,
    "github.get_pull_request": github_provider.get_pull_request,
    "github.list_issues": github_provider.list_issues,
    "github.get_ci_status": github_provider.get_ci_status,
}


def canonical_args(model: BaseModel) -> bytes:
    return json.dumps(
        model.model_dump(mode="json", by_alias=True), sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def default_idempotency_key(
    principal: Principal, spec: ToolSpec, args_hash: bytes, ctx: ExecutionContext
) -> str:
    parts = "|".join(
        [
            str(principal.tenant_id),
            str(principal.user_id),
            str(ctx.run_id or ""),
            str(ctx.step_id or ""),
            spec.name,
            args_hash.hex(),
        ]
    )
    return hashlib.sha256(parts.encode("utf-8")).hexdigest()


def _sanitize(value: Any) -> Any:
    if isinstance(value, str):
        cleaned = "".join(
            ch
            for ch in value
            if ch in "\n\t" or (ord(ch) >= 32 and ch not in "\u200b\u200c\u200d\ufeff")
        )
        return cleaned
    if isinstance(value, dict):
        return {key: _sanitize(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_sanitize(item) for item in value]
    return value


def _result_summary(output: BaseModel) -> dict[str, Any]:
    summary: dict[str, Any] = {"type": type(output).__name__}
    for key, value in output.model_dump(mode="json").items():
        if isinstance(value, list):
            summary[f"{key}_count"] = len(value)
        elif isinstance(value, str):
            summary[key] = value[:120]
        elif isinstance(value, (int, float, bool)) or value is None:
            summary[key] = value
    return summary


async def _integration_for(
    db: AsyncSession, principal: Principal, provider: str
) -> Integration | None:
    return (
        await db.execute(
            select(Integration)
            .where(
                Integration.tenant_id == principal.tenant_id,
                Integration.user_id == principal.user_id,
                Integration.provider == provider,
            )
            .order_by(Integration.connected_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def _access_token(
    db: AsyncSession,
    principal: Principal,
    settings: Settings,
    integration: Integration,
    ctx: ExecutionContext,
) -> tuple[str, str | None]:
    credential, access, refresh = await oauth.load_credential(
        db, principal, settings, integration.id
    )
    expires = credential.access_expires_at
    if refresh and expires is not None and expires <= datetime.now(UTC) + timedelta(seconds=45):
        refreshed = await oauth.refresh_credentials(
            db,
            principal,
            settings,
            provider=integration.provider,
            integration_id=integration.id,
            client=ctx.http,
        )
        if not refreshed:
            raise AppError(
                "Integration credentials could not be refreshed; reconnect required.",
                status_code=403,
                code="needs_reauth",
                title="Reconnect required",
            )
        _, access, refresh = await oauth.load_credential(db, principal, settings, integration.id)
    return access, refresh


async def _existing_execution(
    db: AsyncSession, principal: Principal, spec: ToolSpec, idem_key: str
) -> ToolExecution | None:
    return (
        await db.execute(
            select(ToolExecution).where(
                ToolExecution.tenant_id == principal.tenant_id,
                ToolExecution.tool_name == spec.name,
                ToolExecution.idempotency_key == idem_key,
            )
        )
    ).scalar_one_or_none()


async def _claim_execution(
    db: AsyncSession,
    principal: Principal,
    spec: ToolSpec,
    *,
    args: dict[str, Any],
    args_hash: bytes,
    idem_key: str,
    decision: PolicyDecision,
    ctx: ExecutionContext,
) -> tuple[ToolExecution, bool]:
    stmt = (
        pg_insert(ToolExecution)
        .values(
            tenant_id=principal.tenant_id,
            user_id=principal.user_id,
            run_id=ctx.run_id,
            step_id=ctx.step_id,
            tool_name=spec.name,
            tool_version=spec.version,
            risk_tier=spec.risk_tier,
            args=args,
            args_hash=args_hash,
            idempotency_key=idem_key,
            policy_decision=decision.decision,
            policy_reasons=[decision.reason],
            status="pending",
            taint_in=ctx.run_tainted,
        )
        .on_conflict_do_nothing(index_elements=["tenant_id", "tool_name", "idempotency_key"])
        .returning(ToolExecution)
    )
    row = (await db.execute(stmt)).scalar_one_or_none()
    if row is not None:
        return row, False
    existing = await _existing_execution(db, principal, spec, idem_key)
    if existing is None:  # pragma: no cover - unique conflict without visible row
        raise conflict("Could not claim tool execution.", code="tool_execution_race")
    return existing, True


async def _create_approval(
    db: AsyncSession,
    principal: Principal,
    spec: ToolSpec,
    execution: ToolExecution,
    args: dict[str, Any],
    args_hash: bytes,
    decision: PolicyDecision,
    ctx: ExecutionContext,
) -> Approval:
    ttl = timedelta(hours=1) if spec.risk_tier >= 4 else timedelta(hours=24)
    approval = Approval(
        tenant_id=principal.tenant_id,
        user_id=principal.user_id,
        run_id=ctx.run_id,
        tool_execution_id=execution.id,
        tool_name=spec.name,
        risk_tier=spec.risk_tier,
        args_hash=args_hash,
        preview={
            **ctx.preview_renderer(spec.preview_renderer or spec.name, args),
            "_args": args,
        },
        reasons=[decision.reason],
        status="pending",
        expires_at=datetime.now(UTC) + ttl,
    )
    db.add(approval)
    await db.flush()
    return approval


async def execute(
    db: AsyncSession,
    principal: Principal,
    settings: Settings,
    ctx: ExecutionContext,
    *,
    tool_name: str,
    raw_args: dict[str, Any],
    registry_: ToolRegistry = registry,
) -> ToolResult:
    spec = registry_.get(tool_name)
    if spec is None:
        raise not_found(f"Unknown tool {tool_name!r}.")

    try:
        args = spec.input_model.model_validate(raw_args, strict=True)
    except ValidationError as exc:
        raise validation_failed(
            "Tool arguments failed validation.",
            errors=[
                {"field": ".".join(str(part) for part in error["loc"]), "message": error["msg"]}
                for error in exc.errors()
            ],
        ) from exc

    if spec.name not in {item.name for item in registry_.expose(ctx.domains)}:
        raise AppError(
            "Tool is not allowed in this run.",
            status_code=403,
            code="tool_not_allowed",
            title="Tool not allowed",
        )

    canonical = canonical_args(args)
    args_hash = hashlib.sha256(canonical).digest()
    idem_key = ctx.idempotency_key or default_idempotency_key(principal, spec, args_hash, ctx)

    prior = await _existing_execution(db, principal, spec, idem_key)
    if prior is not None:
        if prior.status == "succeeded" and prior.result_summary is not None:
            return ToolResult("succeeded", prior.id, approval_id=prior.approval_id, replayed=True)
        if prior.status in {"pending", "running"}:
            return ToolResult(
                "pending_approval" if prior.approval_id else "running",
                prior.id,
                approval_id=prior.approval_id,
                reason="idempotent_replay",
                replayed=True,
            )
        if prior.status == "denied":
            return ToolResult("denied", prior.id, reason="idempotent_replay", replayed=True)

    integration_status = "active"
    granted: frozenset[str] = frozenset()
    integration: Integration | None = None
    if spec.integration is not None:
        integration = await _integration_for(db, principal, spec.integration)
        if integration is None or integration.status != "active":
            integration_status = integration.status if integration else "needs_reauth"
        else:
            integration_status = integration.status
            granted = frozenset(integration.granted_scopes)

    policy = decide(
        spec,
        PolicyContext(
            integration_status=integration_status,
            granted_scopes=granted,
            budget_exceeded=ctx.budget_exceeded,
            run_tainted=ctx.run_tainted,
            critical_arg_untrusted=ctx.critical_arg_untrusted,
            autonomy=ctx.autonomy,
            anomaly=ctx.anomaly,
            kill_switches=ctx.kill_switches,
        ),
    )
    execution, replayed = await _claim_execution(
        db,
        principal,
        spec,
        args=args.model_dump(mode="json", by_alias=True),
        args_hash=args_hash,
        idem_key=idem_key,
        decision=policy,
        ctx=ctx,
    )
    if replayed and execution.status == "succeeded":
        return ToolResult("succeeded", execution.id, replayed=True)

    if policy.decision == "deny":
        execution.status = "denied"
        execution.policy_reasons = [policy.reason]
        execution.finished_at = datetime.now(UTC)
        await db.flush()
        await record_event(
            db,
            principal,
            action="tool.denied",
            resource_type="tool_execution",
            resource_id=str(execution.id),
            metadata={"tool": spec.name, "reason": policy.reason, "tier": spec.risk_tier},
        )
        return ToolResult("denied", execution.id, reason=policy.reason, replayed=replayed)

    if policy.decision == "require_approval":
        approval = await _create_approval(
            db,
            principal,
            spec,
            execution,
            args.model_dump(mode="json", by_alias=True),
            args_hash,
            policy,
            ctx,
        )
        execution.approval_id = approval.id
        await db.flush()
        await record_event(
            db,
            principal,
            action="approval.requested",
            resource_type="tool_execution",
            resource_id=str(execution.id),
            metadata={"tool": spec.name, "reason": policy.reason, "tier": spec.risk_tier},
        )
        return ToolResult(
            "pending_approval", execution.id, approval_id=approval.id, reason=policy.reason
        )

    handler = _HANDLERS.get(spec.name)
    if handler is None:
        raise not_found(f"Tool {spec.name!r} has no executor wired.")

    access_token = ""
    if integration is not None:
        try:
            access_token, _ = await _access_token(db, principal, settings, integration, ctx)
        except AppError as exc:
            execution.status = "failed"
            execution.error = {"kind": exc.code, "message": exc.detail}
            execution.finished_at = datetime.now(UTC)
            await db.flush()
            raise

    execution.status = "running"
    execution.started_at = datetime.now(UTC)
    await db.flush()

    try:
        async with asyncio.timeout(spec.timeout_s):
            output = await handler(ctx.http, access_token, args)
        payload = _sanitize(output.model_dump(mode="json", by_alias=True))
        output = spec.output_model.model_validate(payload)
    except (TimeoutError, httpx.HTTPError, AppError) as exc:
        execution.status = "timed_out" if isinstance(exc, TimeoutError) else "failed"
        execution.error = {"kind": type(exc).__name__, "message": str(exc)[:300]}
        execution.finished_at = datetime.now(UTC)
        await db.flush()
        if isinstance(exc, AppError):
            raise
        raise AppError(
            "Tool execution failed.",
            status_code=502,
            code="tool_failed",
            title="Tool execution failed",
        ) from exc
    except Exception as exc:
        execution.status = "failed"
        execution.error = {"kind": type(exc).__name__}
        execution.finished_at = datetime.now(UTC)
        await db.flush()
        raise

    execution.status = "succeeded"
    execution.result_summary = _result_summary(output)
    execution.finished_at = datetime.now(UTC)
    await db.flush()

    await record_event(
        db,
        principal,
        action="tool.executed",
        resource_type="tool_execution",
        resource_id=str(execution.id),
        metadata={
            "tool": spec.name,
            "tier": spec.risk_tier,
            "side_effect": spec.side_effect,
        },
    )
    return ToolResult("succeeded", execution.id, output=output, replayed=replayed)


async def approve(
    db: AsyncSession,
    principal: Principal,
    approval_id: uuid.UUID,
    *,
    args_hash: bytes | None = None,
    edited_args: dict[str, Any] | None = None,
    note: str | None = None,
) -> Approval:
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
        raise not_found("Approval not found.")
    if approval.status != "pending":
        raise conflict("Approval is no longer pending.", code="approval_not_pending")
    if approval.expires_at <= datetime.now(UTC):
        approval.status = "expired"
        await db.flush()
        raise conflict("Approval has expired.", code="approval_expired")

    if edited_args is not None:
        edited_hash = hashlib.sha256(
            json.dumps(edited_args, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).digest()
        spec = registry.require(str(approval.tool_name))
        renderer = render_preview
        approval.preview = {
            **renderer(spec.preview_renderer or spec.name, edited_args),
            "_args": edited_args,
        }
        approval.edited_args_hash = edited_hash
        approval.status = "edited_approved"
    else:
        expected = approval.args_hash
        if args_hash is not None and args_hash != expected:
            raise conflict(
                "Approval does not match the execution arguments.",
                code="approval_args_mismatch",
            )
        approval.status = "approved"

    approval.decided_at = datetime.now(UTC)
    approval.decided_via = "api"
    approval.decision_note = note
    await db.flush()
    await record_event(
        db,
        principal,
        action="approval.decided",
        resource_type="approval",
        resource_id=str(approval.id),
        metadata={"status": approval.status, "tool": approval.tool_name},
    )
    return approval


async def execute_approved(
    db: AsyncSession,
    principal: Principal,
    settings: Settings,
    ctx: ExecutionContext,
    approval_id: uuid.UUID,
    *,
    registry_: ToolRegistry = registry,
) -> ToolResult:
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
        raise not_found("Approval not found.")
    if approval.status not in {"approved", "edited_approved"}:
        raise conflict("Approval has not been granted.", code="approval_not_granted")
    if approval.expires_at <= datetime.now(UTC):
        approval.status = "expired"
        await db.flush()
        raise conflict("Approval has expired.", code="approval_expired")
    if approval.tool_execution_id is None:
        raise conflict("Approval has no execution.", code="approval_without_execution")

    execution = (
        await db.execute(
            select(ToolExecution).where(
                ToolExecution.id == approval.tool_execution_id,
                ToolExecution.tenant_id == principal.tenant_id,
            )
        )
    ).scalar_one_or_none()
    if execution is None:
        raise not_found("Tool execution not found.")

    expected = (
        approval.edited_args_hash if approval.status == "edited_approved" else approval.args_hash
    )
    if expected is None:
        raise conflict(
            "Approval does not match the execution arguments.",
            code="approval_args_mismatch",
        )
    if approval.status == "edited_approved":
        edited_hash = hashlib.sha256(
            json.dumps(
                approval.preview.get("_args") or {},
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).digest()
        if edited_hash != expected:
            raise conflict(
                "Approval does not match the execution arguments.",
                code="approval_args_mismatch",
            )
    elif execution.args_hash != expected:
        raise conflict(
            "Approval does not match the execution arguments.",
            code="approval_args_mismatch",
        )
    if execution.status == "succeeded":
        return ToolResult("succeeded", execution.id, approval_id=approval.id, replayed=True)

    spec = registry_.require(str(approval.tool_name))
    handler = _HANDLERS.get(spec.name)
    if handler is None:
        raise not_found(f"Tool {spec.name!r} has no executor wired.")
    if spec.integration is None:
        raise not_found("Tool has no integration.")
    integration = await _integration_for(db, principal, spec.integration)
    if integration is None:
        raise not_found("Integration is not connected.")
    try:
        access_token, _ = await _access_token(db, principal, settings, integration, ctx)
    except AppError as exc:
        execution.status = "failed"
        execution.error = {"kind": exc.code, "message": exc.detail}
        execution.finished_at = datetime.now(UTC)
        await db.flush()
        raise
    if approval.status == "edited_approved":
        edited = approval.preview.get("_args") or {}
        args = spec.input_model.model_validate(edited, strict=False)
        execution.args = args.model_dump(mode="json", by_alias=True)
        execution.args_hash = expected
    else:
        args = spec.input_model.model_validate(execution.args, strict=False)
    execution.status = "running"
    execution.started_at = datetime.now(UTC)
    await db.flush()
    try:
        async with asyncio.timeout(spec.timeout_s):
            output = await handler(ctx.http, access_token, args)
    except (TimeoutError, httpx.HTTPError, AppError) as exc:
        execution.status = "timed_out" if isinstance(exc, TimeoutError) else "failed"
        execution.error = {"kind": type(exc).__name__, "message": str(exc)[:300]}
        execution.finished_at = datetime.now(UTC)
        await db.flush()
        raise
    execution.status = "succeeded"
    execution.result_summary = _result_summary(output)
    execution.finished_at = datetime.now(UTC)
    await db.flush()
    await record_event(
        db,
        principal,
        action="tool.executed",
        resource_type="tool_execution",
        resource_id=str(execution.id),
        metadata={"tool": spec.name, "tier": spec.risk_tier, "approved": True},
    )
    return ToolResult("succeeded", execution.id, output=output, approval_id=approval.id)
