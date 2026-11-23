"""Unit tests for token/crypto helpers in ``zara.auth.tokens``."""

from __future__ import annotations

import base64
import binascii
from datetime import UTC, datetime, timedelta

import jwt
import pytest
from zara.auth.tokens import (
    SESSION_TOKEN_PREFIX,
    hash_session_token,
    issue_local_token,
    mint_session_token,
    require_bearer,
    step_up_is_fresh,
    verify_access_token,
)
from zara.config import Settings
from zara.errors import AppError


def _local_settings(settings: Settings) -> Settings:
    assert settings.auth_provider == "local"
    return settings


def test_hash_session_token_is_deterministic_sha256() -> None:
    first = hash_session_token("zara_s_alpha")
    second = hash_session_token("zara_s_alpha")

    assert first == second
    assert isinstance(first, bytes)
    assert len(first) == 32


def test_hash_session_token_differs_for_different_inputs() -> None:
    assert hash_session_token("zara_s_alpha") != hash_session_token("zara_s_beta")


def test_mint_session_token_has_prefix_and_at_least_32_bytes_entropy() -> None:
    tokens = [mint_session_token() for _ in range(64)]

    assert len(set(tokens)) == 64
    for token in tokens:
        assert token.startswith(SESSION_TOKEN_PREFIX)
        raw = base64.urlsafe_b64decode(token.removeprefix(SESSION_TOKEN_PREFIX) + "===")
        assert len(raw) >= 32


def test_require_bearer_accepts_case_insensitive_scheme() -> None:
    assert require_bearer("Bearer abc.def") == "abc.def"
    assert require_bearer("bearer   abc.def  ") == "abc.def"


@pytest.mark.parametrize("header", ["", "Basic abc.def", "Bearer", "Bearer    ", "Token abc"])
def test_require_bearer_rejects_missing_or_malformed_credentials(header: str) -> None:
    with pytest.raises(AppError) as excinfo:
        require_bearer(header)

    assert excinfo.value.status_code == 401
    assert excinfo.value.code == "unauthenticated"


def test_require_bearer_rejects_none() -> None:
    with pytest.raises(AppError) as excinfo:
        require_bearer(None)

    assert excinfo.value.code == "unauthenticated"


def test_step_up_is_fresh_boundaries() -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)

    assert step_up_is_fresh(None, now=now) is False
    assert step_up_is_fresh(now - timedelta(minutes=9), now=now) is True
    assert step_up_is_fresh(now - timedelta(minutes=10), now=now) is True
    assert step_up_is_fresh(now - timedelta(minutes=11), now=now) is False
    assert step_up_is_fresh(now + timedelta(minutes=1), now=now) is True


def test_issue_local_token_embeds_required_claims(settings: Settings) -> None:
    resolved = _local_settings(settings)
    token = issue_local_token(
        resolved,
        subject="dev:test@example.com",
        email="test@example.com",
        display_name="Test",
        org_id="org-123",
        scopes=("service",),
    )

    payload = jwt.decode(
        token,
        resolved.auth_local_secret.get_secret_value(),
        algorithms=["HS256"],
        audience=resolved.auth_audience,
        issuer=resolved.auth_issuer,
    )

    assert payload["sub"] == "dev:test@example.com"
    assert payload["iss"] == resolved.auth_issuer
    assert payload["aud"] == resolved.auth_audience
    assert payload["email"] == "test@example.com"
    assert payload["name"] == "Test"
    assert payload["org_id"] == "org-123"
    assert payload["scope"] == "service"
    assert payload["mfa"] is False
    assert {"iat", "exp", "auth_time"} <= set(payload)


def test_issue_local_token_honours_lifetime_seconds(settings: Settings) -> None:
    resolved = _local_settings(settings)
    before = datetime.now(UTC)
    token = issue_local_token(
        resolved,
        subject="dev:test@example.com",
        email="test@example.com",
        lifetime_seconds=60,
        authenticated_at=before,
    )
    payload = jwt.decode(
        token,
        resolved.auth_local_secret.get_secret_value(),
        algorithms=["HS256"],
        audience=resolved.auth_audience,
        issuer=resolved.auth_issuer,
    )

    assert int(payload["exp"]) - int(payload["iat"]) == 60
    assert int(payload["auth_time"]) == int(before.timestamp())


async def test_verify_access_token_round_trips_issued_token(settings: Settings) -> None:
    resolved = _local_settings(settings)
    token = issue_local_token(
        resolved,
        subject="dev:test@example.com",
        email="test@example.com",
        org_id="org-123",
        scopes=("service",),
        authenticated_at=datetime.now(UTC),
    )

    claims = await verify_access_token(token, resolved)

    assert claims.subject == "dev:test@example.com"
    assert claims.email == "test@example.com"
    assert claims.org_id == "org-123"
    assert claims.scopes == ("service",)


async def test_verify_access_token_rejects_non_hs256_algorithm(settings: Settings) -> None:
    """A token signed with HS512 must be rejected by the local provider."""
    resolved = _local_settings(settings)
    now = datetime.now(UTC)
    payload = {
        "sub": "dev:test@example.com",
        "email": "test@example.com",
        "iss": resolved.auth_issuer,
        "aud": resolved.auth_audience,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=15)).timestamp()),
        "scope": "",
    }
    forged = jwt.encode(
        payload,
        "f" * 64,
        algorithm="HS512",
    )

    with pytest.raises(AppError) as excinfo:
        await verify_access_token(forged, resolved)

    assert excinfo.value.status_code == 401
    assert excinfo.value.code == "unauthenticated"


async def test_verify_access_token_rejects_garbage(settings: Settings) -> None:
    with pytest.raises(AppError) as excinfo:
        await verify_access_token("not-a-jwt", _local_settings(settings))

    assert excinfo.value.code == "unauthenticated"


def test_base64_helpers_are_importable() -> None:
    # Guard against accidental shadowing of the crypto imports used above.
    assert binascii.Error is not None
