"""OAuth authorization-code + PKCE flows for Google and GitHub.

Security properties (09-SECURITY-TESTING §1):

* 256-bit random ``state``, single use, 10-minute TTL, hashed at rest;
* the state token is HMAC-signed and carries the tenant/user so the browser
  callback can establish the RLS context without a bearer header;
* PKCE ``S256`` verifier is encrypted with envelope encryption;
* tokens are encrypted before insert and never returned by any endpoint;
* refresh is serialised with a Postgres advisory lock; ``invalid_grant``
  moves the integration to ``needs_reauth``.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal
from urllib.parse import urlencode

import httpx
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from zara.auth.principal import Principal
from zara.config import Settings
from zara.errors import AppError, conflict, upstream_error, validation_failed
from zara.models import Integration, OAuthCredential, OAuthState
from zara.security import (
    CredentialDecryptionError,
    credential_aad,
    decrypt_secret,
    encrypt_many,
    encrypt_secret,
    state_aad,
)
from zara.services.audit import record_event

Provider = Literal["gmail", "gcal", "gdrive", "github"]
ProviderKind = Literal["google", "github"]

_GOOGLE_AUTHORIZE = "https://accounts.google.com/o/oauth2/v2/auth"
_GOOGLE_CODE_EXCHANGE = "https://oauth2.googleapis.com/token"
_GOOGLE_USERINFO = "https://openidconnect.googleapis.com/v1/userinfo"
_GOOGLE_REVOKE = "https://oauth2.googleapis.com/revoke"
_GITHUB_AUTHORIZE = "https://github.com/login/oauth/authorize"
_GITHUB_CODE_EXCHANGE = "https://github.com/login/oauth/access_token"
_GITHUB_USER = "https://api.github.com/user"
_GITHUB_EMAILS = "https://api.github.com/user/emails"
_GITHUB_REVOKE = "https://api.github.com/applications/{client_id}/token"


@dataclass(frozen=True, slots=True)
class ProviderConfig:
    name: Provider
    kind: ProviderKind
    scopes: tuple[str, ...]


PROVIDERS: dict[Provider, ProviderConfig] = {
    "gmail": ProviderConfig(
        "gmail",
        "google",
        (
            "openid",
            "email",
            "profile",
            "https://www.googleapis.com/auth/gmail.readonly",
            "https://www.googleapis.com/auth/gmail.compose",
        ),
    ),
    "gcal": ProviderConfig(
        "gcal",
        "google",
        (
            "openid",
            "email",
            "profile",
            "https://www.googleapis.com/auth/calendar.events",
        ),
    ),
    "gdrive": ProviderConfig(
        "gdrive",
        "google",
        (
            "openid",
            "email",
            "profile",
            "https://www.googleapis.com/auth/drive.file",
        ),
    ),
    # GitHub App user-to-server tokens carry the app's configured repository
    # permissions (read-only in MVP), not OAuth scopes.
    "github": ProviderConfig("github", "github", ("repo:read",)),
}


@dataclass(frozen=True, slots=True)
class Authorization:
    authorization_url: str
    state: str
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class Connected:
    integration: Integration
    redirect_after: str | None


def _now() -> datetime:
    return datetime.now(UTC)


def _pkce_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def _state_signature(settings: Settings, payload: str) -> str:
    key = settings.oauth_state_signing_key.get_secret_value().encode("utf-8")
    return hmac.new(key, payload.encode("ascii"), hashlib.sha256).hexdigest()


def encode_state(settings: Settings, raw: str, tenant_id: uuid.UUID, user_id: uuid.UUID) -> str:
    payload = f"{raw}.{tenant_id}.{user_id}"
    return f"{payload}.{_state_signature(settings, payload)}"


def decode_state(settings: Settings, state: str) -> tuple[str, uuid.UUID, uuid.UUID]:
    parts = state.rsplit(".", 3)
    if len(parts) != 4:
        raise validation_failed("Invalid OAuth state.")
    raw, tenant_text, user_text, signature = parts
    payload = f"{raw}.{tenant_text}.{user_text}"
    expected = _state_signature(settings, payload)
    if not hmac.compare_digest(expected, signature):
        raise validation_failed("Invalid OAuth state signature.")
    try:
        return raw, uuid.UUID(tenant_text), uuid.UUID(user_text)
    except ValueError as exc:
        raise validation_failed("Invalid OAuth state.") from exc


def _provider(provider: str) -> ProviderConfig:
    if provider not in PROVIDERS:
        raise validation_failed(
            "Unknown integration provider.",
            errors=[{"field": "provider", "message": f"Expected one of {sorted(PROVIDERS)}"}],
        )
    return PROVIDERS[provider]


def _google_credentials(settings: Settings) -> tuple[str, str]:
    client_id = settings.google_oauth_client_id
    secret = settings.google_oauth_client_secret
    if not client_id or secret is None:
        raise validation_failed("Google OAuth is not configured on this deployment.")
    return client_id, secret.get_secret_value()


def _github_credentials(settings: Settings) -> tuple[str, str]:
    client_id = settings.github_oauth_client_id
    secret = settings.github_oauth_client_secret
    if not client_id or secret is None:
        raise validation_failed("GitHub OAuth is not configured on this deployment.")
    return client_id, secret.get_secret_value()


def redirect_uri(settings: Settings, provider: str) -> str:
    base = str(settings.oauth_redirect_base_url).rstrip("/")
    return f"{base}/api/v1/integrations/{provider}/callback"


def _safe_redirect_after(value: str | None) -> str | None:
    if not value:
        return None
    # Only same-origin relative paths are accepted; absolute URLs are dropped to
    # prevent an open redirect after the callback.
    if value.startswith("/") and not value.startswith("//"):
        return value
    return None


async def begin_connect(
    db: AsyncSession,
    principal: Principal,
    settings: Settings,
    *,
    provider: str,
    scopes: list[str] | None = None,
    redirect_after: str | None = None,
) -> Authorization:
    config = _provider(provider)
    if config.kind == "google":
        client_id, _ = _google_credentials(settings)
    else:
        client_id, _ = _github_credentials(settings)

    requested = tuple(scopes or config.scopes)
    invalid = sorted(set(requested) - set(config.scopes))
    if invalid:
        raise validation_failed(
            "Requested scopes are not allowed for this provider.",
            errors=[{"field": "scopes", "message": f"Disallowed: {invalid}"}],
        )
    if not requested:
        raise validation_failed("At least one scope is required.")

    raw_state = secrets.token_urlsafe(32)
    state_hash = hashlib.sha256(raw_state.encode("ascii")).digest()
    verifier = secrets.token_urlsafe(64)
    challenge = _pkce_challenge(verifier)
    expires_at = _now() + timedelta(seconds=settings.oauth_state_ttl_seconds)

    encrypted = encrypt_secret(
        settings,
        plaintext=verifier.encode("ascii"),
        aad=state_aad(principal.tenant_id, principal.user_id, config.name, state_hash),
    )
    db.add(
        OAuthState(
            tenant_id=principal.tenant_id,
            user_id=principal.user_id,
            state_hash=state_hash,
            provider=config.name,
            requested_scopes=list(requested),
            code_verifier_ct=encrypted.ciphertext,
            dek_wrapped=encrypted.dek_wrapped,
            kms_key_id=encrypted.kms_key_id,
            enc_version=encrypted.enc_version,
            redirect_after=_safe_redirect_after(redirect_after),
            expires_at=expires_at,
        )
    )
    await db.flush()

    state = encode_state(settings, raw_state, principal.tenant_id, principal.user_id)
    if config.kind == "google":
        params = {
            "client_id": client_id,
            "redirect_uri": redirect_uri(settings, config.name),
            "response_type": "code",
            "scope": " ".join(requested),
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "access_type": "offline",
            "prompt": "consent",
            "include_granted_scopes": "true",
        }
        url = f"{_GOOGLE_AUTHORIZE}?{urlencode(params)}"
    else:
        params = {
            "client_id": client_id,
            "redirect_uri": redirect_uri(settings, config.name),
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
        url = f"{_GITHUB_AUTHORIZE}?{urlencode(params)}"

    await record_event(
        db,
        principal,
        action="integration.connect_started",
        resource_type="integration",
        metadata={"provider": config.name, "scopes": sorted(requested)},
    )
    return Authorization(authorization_url=url, state=state, expires_at=expires_at)


async def _exchange_code(
    settings: Settings,
    config: ProviderConfig,
    *,
    code: str,
    verifier: str,
    client: httpx.AsyncClient,
) -> dict[str, Any]:
    if config.kind == "google":
        client_id, secret = _google_credentials(settings)
        data = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri(settings, config.name),
            "client_id": client_id,
            "client_secret": secret,
            "code_verifier": verifier,
        }
        response = await client.post(_GOOGLE_CODE_EXCHANGE, data=data)
    else:
        client_id, secret = _github_credentials(settings)
        data = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri(settings, config.name),
            "client_id": client_id,
            "client_secret": secret,
            "code_verifier": verifier,
        }
        response = await client.post(
            _GITHUB_CODE_EXCHANGE, data=data, headers={"Accept": "application/json"}
        )
    if response.status_code >= 400:
        # Never echo the provider body: it can contain tokens or client ids.
        raise upstream_error(f"Token exchange failed (status {response.status_code}).")
    try:
        payload: dict[str, Any] = response.json()
    except ValueError as exc:
        raise upstream_error("Token exchange returned a non-JSON response.") from exc
    if "error" in payload:
        raise upstream_error("The provider rejected the authorization code.")
    if not payload.get("access_token"):
        raise upstream_error("The provider did not return an access token.")
    return payload


async def _fetch_identity(
    config: ProviderConfig, access_token: str, client: httpx.AsyncClient
) -> tuple[str, str | None]:
    headers = {"Authorization": f"Bearer {access_token}", "Accept": "application/json"}
    if config.kind == "google":
        response = await client.get(_GOOGLE_USERINFO, headers=headers)
        if response.status_code >= 400:
            raise upstream_error("Could not read the Google account profile.")
        body: dict[str, Any] = response.json()
        account = body.get("email") or body.get("sub")
        if not account:
            raise upstream_error("Google profile did not include an account identifier.")
        return str(account), body.get("sub")

    response = await client.get(_GITHUB_USER, headers=headers)
    if response.status_code >= 400:
        raise upstream_error("Could not read the GitHub account profile.")
    body = response.json()
    account = body.get("email") or body.get("login")
    if not account:
        raise upstream_error("GitHub profile did not include an account identifier.")
    external_id = str(body.get("id")) if body.get("id") is not None else None
    return str(account), external_id


def _store_credential(
    db: AsyncSession,
    principal: Principal,
    settings: Settings,
    integration: Integration,
    *,
    access_token: str,
    refresh_token: str | None,
    token_type: str | None,
    expires_in: int | None,
) -> OAuthCredential:
    aad = credential_aad(principal.tenant_id, principal.user_id, integration.id)
    plaintexts = {"access": access_token.encode("utf-8")}
    if refresh_token:
        plaintexts["refresh"] = refresh_token.encode("utf-8")
    sealed = encrypt_many(settings, plaintexts=plaintexts, aad=aad)
    credential = OAuthCredential(
        tenant_id=principal.tenant_id,
        integration_id=integration.id,
        token_type=token_type,
        access_token_ct=sealed.ciphertexts["access"],
        refresh_token_ct=sealed.ciphertexts.get("refresh"),
        access_expires_at=(_now() + timedelta(seconds=expires_in)) if expires_in else None,
        dek_wrapped=sealed.dek_wrapped,
        kms_key_id=sealed.kms_key_id,
        enc_version=sealed.enc_version,
        last_refreshed_at=_now(),
    )
    db.add(credential)
    return credential


async def complete_connect(
    db: AsyncSession,
    principal: Principal,
    settings: Settings,
    *,
    provider: str,
    code: str,
    state: str,
    client: httpx.AsyncClient,
) -> Connected:
    config = _provider(provider)
    raw_state, state_tenant, state_user = decode_state(settings, state)
    if state_tenant != principal.tenant_id or state_user != principal.user_id:
        raise validation_failed("OAuth state does not belong to this user.")

    state_hash = hashlib.sha256(raw_state.encode("ascii")).digest()
    row = (
        await db.execute(
            select(OAuthState)
            .where(
                OAuthState.state_hash == state_hash,
                OAuthState.tenant_id == principal.tenant_id,
                OAuthState.user_id == principal.user_id,
                OAuthState.provider == config.name,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if row is None:
        raise validation_failed("Invalid or expired OAuth state.")
    if row.used_at is not None:
        raise conflict("OAuth state has already been used.", code="oauth_state_used")
    if row.expires_at <= _now():
        raise validation_failed("OAuth state has expired.")

    try:
        verifier = decrypt_secret(
            settings,
            ciphertext=row.code_verifier_ct,
            dek_wrapped=row.dek_wrapped,
            aad=state_aad(principal.tenant_id, principal.user_id, config.name, state_hash),
        ).decode("ascii")
    except CredentialDecryptionError as exc:
        raise validation_failed("OAuth state could not be decrypted.") from exc

    token_body = await _exchange_code(settings, config, code=code, verifier=verifier, client=client)
    access_token = str(token_body["access_token"])
    refresh_token = token_body.get("refresh_token")
    account_identifier, external_id = await _fetch_identity(config, access_token, client)

    granted = token_body.get("scope")
    granted_scopes = (
        [scope for scope in str(granted).split() if scope]
        if granted
        else list(row.requested_scopes)
    )

    integration = (
        await db.execute(
            select(Integration).where(
                Integration.tenant_id == principal.tenant_id,
                Integration.user_id == principal.user_id,
                Integration.provider == config.name,
                Integration.account_identifier == account_identifier,
            )
        )
    ).scalar_one_or_none()
    if integration is None:
        integration = Integration(
            tenant_id=principal.tenant_id,
            user_id=principal.user_id,
            provider=config.name,
            account_identifier=account_identifier,
            status="active",
            granted_scopes=granted_scopes,
            connected_at=_now(),
        )
        db.add(integration)
        await db.flush()
    else:
        integration.status = "active"
        integration.granted_scopes = granted_scopes
        integration.connected_at = integration.connected_at or _now()
        integration.revoked_at = None
        integration.last_error = None
        integration.error_count = 0
    integration.external_account_id = external_id

    existing = (
        await db.execute(
            select(OAuthCredential).where(
                OAuthCredential.tenant_id == principal.tenant_id,
                OAuthCredential.integration_id == integration.id,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        await db.delete(existing)
        await db.flush()

    _store_credential(
        db,
        principal,
        settings,
        integration,
        access_token=access_token,
        refresh_token=str(refresh_token) if refresh_token else None,
        token_type=token_body.get("token_type"),
        expires_in=token_body.get("expires_in"),
    )
    row.used_at = _now()
    await db.flush()

    await record_event(
        db,
        principal,
        action="integration.connected",
        resource_type="integration",
        resource_id=str(integration.id),
        metadata={"provider": config.name, "scopes": sorted(granted_scopes)},
    )
    return Connected(integration=integration, redirect_after=row.redirect_after)


async def load_credential(
    db: AsyncSession, principal: Principal, settings: Settings, integration_id: uuid.UUID
) -> tuple[OAuthCredential, str, str | None]:
    credential = (
        await db.execute(
            select(OAuthCredential).where(
                OAuthCredential.tenant_id == principal.tenant_id,
                OAuthCredential.integration_id == integration_id,
            )
        )
    ).scalar_one_or_none()
    if credential is None:
        raise validation_failed("Integration has no stored credentials.")
    aad = credential_aad(principal.tenant_id, principal.user_id, integration_id)
    try:
        access = decrypt_secret(
            settings,
            ciphertext=credential.access_token_ct or b"",
            dek_wrapped=credential.dek_wrapped,
            aad=aad,
        ).decode("utf-8")
        refresh = (
            decrypt_secret(
                settings,
                ciphertext=credential.refresh_token_ct,
                dek_wrapped=credential.dek_wrapped,
                aad=aad,
            ).decode("utf-8")
            if credential.refresh_token_ct
            else None
        )
    except CredentialDecryptionError as exc:
        raise validation_failed("Stored credentials could not be decrypted.") from exc
    return credential, access, refresh


async def refresh_credentials(
    db: AsyncSession,
    principal: Principal,
    settings: Settings,
    *,
    provider: str,
    integration_id: uuid.UUID,
    client: httpx.AsyncClient,
) -> bool:
    config = _provider(provider)
    integration = (
        await db.execute(
            select(Integration).where(
                Integration.id == integration_id,
                Integration.tenant_id == principal.tenant_id,
                Integration.user_id == principal.user_id,
            )
        )
    ).scalar_one_or_none()
    if integration is None:
        return False

    lock_key = f"oauth-refresh:{integration.id}"
    acquired: bool = (
        await db.execute(
            text("SELECT pg_try_advisory_xact_lock(hashtextextended(:key, 0))"),
            {"key": lock_key},
        )
    ).scalar_one()
    if not acquired:
        raise conflict("A token refresh is already in progress.", code="refresh_in_progress")

    credential = (
        await db.execute(
            select(OAuthCredential).where(
                OAuthCredential.tenant_id == principal.tenant_id,
                OAuthCredential.integration_id == integration.id,
            )
        )
    ).scalar_one_or_none()
    if credential is None or credential.refresh_token_ct is None:
        integration.status = "needs_reauth"
        integration.last_error = "missing_refresh_token"
        await db.flush()
        return False

    aad = credential_aad(principal.tenant_id, principal.user_id, integration.id)
    try:
        refresh_token = decrypt_secret(
            settings,
            ciphertext=credential.refresh_token_ct,
            dek_wrapped=credential.dek_wrapped,
            aad=aad,
        ).decode("utf-8")
    except CredentialDecryptionError:
        integration.status = "needs_reauth"
        integration.last_error = "credential_decryption_failed"
        await db.flush()
        return False

    if config.kind == "google":
        client_id, secret = _google_credentials(settings)
        data = {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": client_id,
            "client_secret": secret,
        }
        response = await client.post(_GOOGLE_CODE_EXCHANGE, data=data)
    else:
        client_id, secret = _github_credentials(settings)
        data = {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": client_id,
            "client_secret": secret,
        }
        response = await client.post(
            _GITHUB_CODE_EXCHANGE, data=data, headers={"Accept": "application/json"}
        )

    if response.status_code >= 400:
        integration.status = "needs_reauth"
        integration.last_error = "invalid_grant"
        await db.flush()
        return False
    body: dict[str, Any] = response.json()
    if "error" in body or not body.get("access_token"):
        integration.status = "needs_reauth"
        integration.last_error = "invalid_grant"
        await db.flush()
        return False

    access_token = str(body["access_token"])
    rotated = body.get("refresh_token")
    plaintexts = {"access": access_token.encode("utf-8")}
    if rotated:
        plaintexts["refresh"] = str(rotated).encode("utf-8")
    sealed = encrypt_many(settings, plaintexts=plaintexts, aad=aad)
    credential.access_token_ct = sealed.ciphertexts["access"]
    credential.dek_wrapped = sealed.dek_wrapped
    credential.kms_key_id = sealed.kms_key_id
    credential.enc_version = sealed.enc_version
    if rotated:
        credential.refresh_token_ct = sealed.ciphertexts["refresh"]
    if body.get("expires_in"):
        credential.access_expires_at = _now() + timedelta(seconds=int(body["expires_in"]))
    credential.last_refreshed_at = _now()
    integration.last_error = None
    await db.flush()
    await record_event(
        db,
        principal,
        action="integration.token_refreshed",
        resource_type="integration",
        resource_id=str(integration.id),
        metadata={"provider": config.name},
    )
    return True


async def revoke_at_provider(
    settings: Settings,
    provider: str,
    *,
    access_token: str,
    client: httpx.AsyncClient,
) -> None:
    """Best-effort provider-side revocation. Never raises or logs the token."""
    config = _provider(provider)
    try:
        if config.kind == "google":
            await client.post(_GOOGLE_REVOKE, data={"token": access_token})
        else:
            client_id, secret = _github_credentials(settings)
            await client.request(
                "DELETE",
                _GITHUB_REVOKE.format(client_id=client_id),
                json={"access_token": access_token},
                auth=(client_id, secret),
            )
    except (httpx.HTTPError, AppError):
        return
