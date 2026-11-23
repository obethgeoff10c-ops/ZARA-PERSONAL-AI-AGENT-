"""Bearer-token verification and opaque session token helpers.

Two token shapes are accepted on the API:

* An IdP access JWT (RS256/ES256) verified against a cached JWKS, or an HS256
  token from the development-only local provider.
* An opaque ``zara_s_…`` session token minted by ``POST /auth/session``. Only
  its SHA-256 digest is stored (``core.auth_sessions.session_token_hash``).

Both paths end in the same :class:`~zara.auth.principal.Principal`.
"""

from __future__ import annotations

import hashlib
import secrets
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import jwt
from jwt import PyJWK

from zara.config import Settings
from zara.errors import unauthenticated

SESSION_TOKEN_PREFIX = "zara_s_"  # noqa: S105 - token prefix, not a credential
_STEP_UP_WINDOW = timedelta(minutes=10)


@dataclass(frozen=True, slots=True)
class TokenClaims:
    subject: str
    email: str | None
    email_verified: bool
    display_name: str | None
    org_id: str | None
    scopes: tuple[str, ...]
    authenticated_at: datetime | None
    mfa: bool
    raw: dict[str, Any]


def hash_session_token(token: str) -> bytes:
    return hashlib.sha256(token.encode("utf-8")).digest()


def mint_session_token() -> str:
    return SESSION_TOKEN_PREFIX + secrets.token_urlsafe(32)


def is_session_token(token: str) -> bool:
    return token.startswith(SESSION_TOKEN_PREFIX)


def require_bearer(authorization: str | None) -> str:
    if not authorization:
        raise unauthenticated()
    scheme, _, credentials = authorization.partition(" ")
    if scheme.lower() != "bearer" or not credentials.strip():
        raise unauthenticated("Malformed Authorization header; expected 'Bearer <token>'.")
    return credentials.strip()


class JwksCache:
    """Small async JWKS cache keyed by ``kid`` with a bounded TTL."""

    def __init__(self, url: str, *, ttl_seconds: int = 300) -> None:
        self._url = url
        self._ttl = ttl_seconds
        self._keys: dict[str, PyJWK] = {}
        self._fetched_at: float = 0.0

    async def get(self, kid: str) -> PyJWK:
        now = time.monotonic()
        if not self._keys or now - self._fetched_at > self._ttl:
            await self._refresh()
        key = self._keys.get(kid)
        if key is None:
            # A rotation may have happened since the last fetch; force one retry.
            await self._refresh()
            key = self._keys.get(kid)
        if key is None:
            raise unauthenticated("The signing key for this token is not recognised.")
        return key

    async def _refresh(self) -> None:
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                response = await client.get(self._url)
                response.raise_for_status()
                payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise unauthenticated("Unable to verify the access token right now.") from exc
        keys: dict[str, PyJWK] = {}
        for entry in payload.get("keys", []):
            if not isinstance(entry, dict) or "kid" not in entry:
                continue
            try:
                keys[str(entry["kid"])] = PyJWK(entry)
            except (ValueError, KeyError):
                continue
        if not keys:
            raise unauthenticated("The identity provider returned no usable signing keys.")
        self._keys = keys
        self._fetched_at = time.monotonic()


_JWKS_CACHES: dict[str, JwksCache] = {}


def _jwk_cache(settings: Settings) -> JwksCache:
    if settings.auth_jwks_url is None:
        raise unauthenticated("AUTH_JWKS_URL is not configured.")
    url = str(settings.auth_jwks_url)
    cache = _JWKS_CACHES.get(url)
    if cache is None:
        cache = JwksCache(url, ttl_seconds=settings.auth_jwks_cache_seconds)
        _JWKS_CACHES[url] = cache
    return cache


def _claims_from(payload: dict[str, Any]) -> TokenClaims:
    subject = payload.get("sub")
    if not isinstance(subject, str) or not subject:
        raise unauthenticated("The access token has no subject.")

    auth_time = payload.get("auth_time")
    authenticated_at: datetime | None = None
    if isinstance(auth_time, (int, float)):
        authenticated_at = datetime.fromtimestamp(float(auth_time), tz=UTC)

    scopes: tuple[str, ...] = ()
    raw_scope = payload.get("scope") or payload.get("scp")
    if isinstance(raw_scope, str):
        scopes = tuple(item for item in raw_scope.split() if item)
    elif isinstance(raw_scope, list):
        scopes = tuple(str(item) for item in raw_scope)

    amr = payload.get("amr")
    mfa = bool(payload.get("mfa") or (isinstance(amr, list) and "mfa" in amr))

    display_name = payload.get("name") or payload.get("display_name")
    email = payload.get("email")
    email_verified = bool(payload.get("email_verified") or payload.get("verified_email"))

    return TokenClaims(
        subject=subject,
        email=str(email) if isinstance(email, str) and email else None,
        email_verified=email_verified,
        display_name=str(display_name) if isinstance(display_name, str) else None,
        org_id=str(payload["org_id"]) if payload.get("org_id") else None,
        scopes=scopes,
        authenticated_at=authenticated_at,
        mfa=mfa,
        raw=payload,
    )


async def verify_access_token(token: str, settings: Settings) -> TokenClaims:
    """Verify signature, issuer, audience and time claims."""
    try:
        header = jwt.get_unverified_header(token)
    except jwt.PyJWTError as exc:
        raise unauthenticated("The access token is malformed.") from exc

    algorithm = str(header.get("alg", ""))
    if settings.auth_provider == "local":
        if algorithm != "HS256":
            raise unauthenticated("The local provider only accepts HS256 tokens.")
        try:
            payload = jwt.decode(
                token,
                settings.auth_local_secret.get_secret_value(),
                algorithms=["HS256"],
                audience=settings.auth_audience or None,
                issuer=settings.auth_issuer or None,
                options={"require": ["exp", "iat", "sub", "iss", "aud"]},
            )
        except jwt.ExpiredSignatureError as exc:
            raise unauthenticated("The access token has expired.") from exc
        except jwt.PyJWTError as exc:
            raise unauthenticated("The access token could not be verified.") from exc
        return _claims_from(payload)

    if algorithm not in settings.auth_jwt_algorithms:
        raise unauthenticated("The access token uses an unexpected signing algorithm.")
    kid = header.get("kid")
    if not isinstance(kid, str) or not kid:
        raise unauthenticated("The access token has no key id.")

    jwk = await _jwk_cache(settings).get(kid)
    try:
        payload = jwt.decode(
            token,
            jwk.key,
            algorithms=[algorithm],
            audience=settings.auth_audience or None,
            issuer=settings.auth_issuer or None,
            options={
                "require": ["exp", "iat", "sub", "iss"],
                "verify_aud": bool(settings.auth_audience),
            },
        )
    except jwt.ExpiredSignatureError as exc:
        raise unauthenticated("The access token has expired.") from exc
    except jwt.PyJWTError as exc:
        raise unauthenticated("The access token could not be verified.") from exc
    return _claims_from(payload)


def issue_local_token(
    settings: Settings,
    *,
    subject: str,
    email: str,
    display_name: str | None = None,
    org_id: str | None = None,
    scopes: tuple[str, ...] = (),
    lifetime_seconds: int = 900,
    authenticated_at: datetime | None = None,
    mfa: bool = False,
) -> str:
    """Mint a development token. Only reachable when ``AUTH_PROVIDER=local``."""
    if settings.auth_provider != "local":
        raise RuntimeError("Local token issuance is only available for the local provider")

    now = datetime.now(UTC)
    auth_time = authenticated_at or now
    payload: dict[str, Any] = {
        "sub": subject,
        "email": email,
        "email_verified": True,
        "name": display_name,
        "iss": settings.auth_issuer,
        "aud": settings.auth_audience,
        "iat": int(now.timestamp()),
        "nbf": int(now.timestamp()),
        "exp": int((now + timedelta(seconds=lifetime_seconds)).timestamp()),
        "auth_time": int(auth_time.timestamp()),
        "scope": " ".join(scopes),
        "mfa": mfa,
    }
    if org_id:
        payload["org_id"] = org_id
    return jwt.encode(
        payload,
        settings.auth_local_secret.get_secret_value(),
        algorithm="HS256",
    )


def step_up_is_fresh(authenticated_at: datetime | None, *, now: datetime | None = None) -> bool:
    if authenticated_at is None:
        return False
    current = now or datetime.now(UTC)
    return current - authenticated_at <= _STEP_UP_WINDOW
