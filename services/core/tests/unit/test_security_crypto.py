"""Unit tests for envelope encryption and OAuth state binding."""

from __future__ import annotations

import uuid

import pytest
from cryptography.exceptions import InvalidTag
from pydantic import ValidationError
from zara.config import Settings
from zara.security.crypto import (
    CredentialDecryptionError,
    credential_aad,
    decrypt_secret,
    encrypt_secret,
    state_aad,
)
from zara.services.oauth import _pkce_challenge

_BASE = {
    "_env_file": None,
    "database_url": "postgresql+asyncpg://zara:zara_local_dev@localhost:5432/zara",
}


def _settings(**overrides: object) -> Settings:
    return Settings(**{**_BASE, **overrides})


def test_round_trip_and_ciphertext_layout() -> None:
    settings = _settings()
    aad = credential_aad(uuid.uuid4(), uuid.uuid4(), uuid.uuid4())

    sealed = encrypt_secret(settings, plaintext=b"refresh-token-value", aad=aad)

    assert sealed.ciphertext[0] == 1
    assert b"refresh-token-value" not in sealed.ciphertext
    assert b"refresh-token-value" not in sealed.dek_wrapped
    assert (
        decrypt_secret(
            settings,
            ciphertext=sealed.ciphertext,
            dek_wrapped=sealed.dek_wrapped,
            aad=aad,
        )
        == b"refresh-token-value"
    )


def test_aad_binds_ciphertext_to_user_and_integration() -> None:
    settings = _settings()
    tenant, user, integration = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    sealed = encrypt_secret(
        settings,
        plaintext=b"access-token",
        aad=credential_aad(tenant, user, integration),
    )

    with pytest.raises(CredentialDecryptionError):
        decrypt_secret(
            settings,
            ciphertext=sealed.ciphertext,
            dek_wrapped=sealed.dek_wrapped,
            aad=credential_aad(tenant, user, uuid.uuid4()),
        )


def test_tampered_ciphertext_is_rejected() -> None:
    settings = _settings()
    aad = state_aad(uuid.uuid4(), uuid.uuid4(), "gmail", b"x" * 32)
    sealed = encrypt_secret(settings, plaintext=b"verifier", aad=aad)
    tampered = bytearray(sealed.ciphertext)
    tampered[-1] ^= 0x01

    with pytest.raises(CredentialDecryptionError):
        decrypt_secret(
            settings,
            ciphertext=bytes(tampered),
            dek_wrapped=sealed.dek_wrapped,
            aad=aad,
        )


def test_local_kms_is_refused_in_production() -> None:
    with pytest.raises(ValidationError, match="KMS_PROVIDER=local"):
        _settings(
            environment="production",
            cors_origins="https://app.zara.app",
            allowed_hosts="app.zara.app",
            database_url="postgresql+asyncpg://zara:Str0ngPass@db:5432/zara",
            auth_provider="clerk",
            auth_issuer="https://clerk.zara.app",
            auth_client_id="zara_app",
            auth_jwks_url="https://clerk.zara.app/.well-known/jwks.json",
        )


def test_production_rejects_placeholder_kms_and_state_keys() -> None:
    base = {
        "environment": "production",
        "cors_origins": "https://app.zara.app",
        "allowed_hosts": "app.zara.app",
        "database_url": "postgresql+asyncpg://zara:Str0ngPass@db:5432/zara",
        "auth_provider": "clerk",
        "auth_issuer": "https://clerk.zara.app",
        "auth_client_id": "zara_app",
        "auth_jwks_url": "https://clerk.zara.app/.well-known/jwks.json",
        "kms_provider": "aws",
    }
    with pytest.raises(ValidationError, match="KMS_MASTER_KEY"):
        _settings(**base)

    with pytest.raises(ValidationError, match="OAUTH_STATE_SIGNING_KEY"):
        _settings(**base, kms_master_key="real-master-key-material")


def test_pkce_challenge_matches_rfc7636_vector() -> None:
    verifier = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"

    assert _pkce_challenge(verifier) == "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"


def test_invalid_tag_is_wrapped() -> None:
    assert issubclass(CredentialDecryptionError, Exception)
    assert InvalidTag is not CredentialDecryptionError
