"""Application-level envelope encryption for OAuth credentials.

Layout follows 09-SECURITY-TESTING §2.2:

* one random AES-256 data-encryption key (DEK) per integration credential;
* the DEK is wrapped by the KMS master key (``local`` provider = env master key
  in dev/tests; AWS/GCP KMS in production);
* every ciphertext is ``version(1) | nonce(12) | ciphertext | tag(16)``;
* the AAD binds ``tenant_id | user_id | integration_id | enc_version`` so a
  ciphertext cannot be moved to another user or integration.
"""

from __future__ import annotations

import hashlib
import os
import uuid
from dataclasses import dataclass

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from zara.config import Settings

_VERSION = 1
_NONCE_BYTES = 12
_KEY_BYTES = 32


class CredentialDecryptionError(Exception):
    """Ciphertext, DEK, or AAD failed authentication."""


@dataclass(frozen=True, slots=True)
class EncryptedSecret:
    ciphertext: bytes
    dek_wrapped: bytes
    kms_key_id: str
    enc_version: int = _VERSION


def _master_key(settings: Settings) -> bytes:
    if settings.kms_provider != "local":
        # Cloud KMS providers wrap/unwrap the DEK themselves; the local
        # AES-GCM fallback must never be used as a stand-in in production.
        raise CredentialDecryptionError(
            f"KMS provider {settings.kms_provider!r} is not wired in this build"
        )
    material = settings.kms_master_key.get_secret_value().encode("utf-8")
    return hashlib.sha256(material).digest()


def credential_aad(
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
    integration_id: uuid.UUID,
    enc_version: int = _VERSION,
) -> bytes:
    return f"{tenant_id}|{user_id}|{integration_id}|{enc_version}".encode("ascii")


def state_aad(
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
    provider: str,
    state_hash: bytes,
) -> bytes:
    return f"{tenant_id}|{user_id}|{provider}|{state_hash.hex()}".encode("ascii")


def _seal(key: bytes, nonce: bytes, plaintext: bytes, aad: bytes) -> bytes:
    return _VERSION.to_bytes(1, "big") + nonce + AESGCM(key).encrypt(nonce, plaintext, aad)


def _open(key: bytes, payload: bytes, aad: bytes) -> bytes:
    if len(payload) < 1 + _NONCE_BYTES + 16:
        raise CredentialDecryptionError("ciphertext is truncated")
    version = payload[0]
    if version != _VERSION:
        raise CredentialDecryptionError(f"unsupported ciphertext version {version}")
    nonce = payload[1 : 1 + _NONCE_BYTES]
    ciphertext = payload[1 + _NONCE_BYTES :]
    try:
        return AESGCM(key).decrypt(nonce, ciphertext, aad)
    except InvalidTag as exc:
        raise CredentialDecryptionError("ciphertext authentication failed") from exc


def encrypt_secret(
    settings: Settings,
    *,
    plaintext: bytes,
    aad: bytes,
) -> EncryptedSecret:
    """Generate a fresh DEK, wrap it, and encrypt ``plaintext`` with it."""
    bundle = encrypt_many(settings, plaintexts={"value": plaintext}, aad=aad)
    return EncryptedSecret(
        ciphertext=bundle.ciphertexts["value"],
        dek_wrapped=bundle.dek_wrapped,
        kms_key_id=bundle.kms_key_id,
    )


@dataclass(frozen=True, slots=True)
class EncryptedBundle:
    """Several ciphertexts sharing one DEK (e.g. access + refresh token)."""

    ciphertexts: dict[str, bytes]
    dek_wrapped: bytes
    kms_key_id: str
    enc_version: int = _VERSION


def encrypt_many(
    settings: Settings,
    *,
    plaintexts: dict[str, bytes],
    aad: bytes,
) -> EncryptedBundle:
    """Encrypt several fields with a single DEK and one wrapped-key copy."""
    if not plaintexts:
        raise ValueError("at least one plaintext is required")
    dek = os.urandom(_KEY_BYTES)
    wrap_nonce = os.urandom(_NONCE_BYTES)
    master = _master_key(settings)
    dek_wrapped = (
        _VERSION.to_bytes(1, "big")
        + wrap_nonce
        + AESGCM(master).encrypt(wrap_nonce, dek, b"zara:dek:v1")
    )
    return EncryptedBundle(
        ciphertexts={
            name: _seal(dek, os.urandom(_NONCE_BYTES), value, aad)
            for name, value in plaintexts.items()
        },
        dek_wrapped=dek_wrapped,
        kms_key_id=settings.kms_key_id,
    )


def decrypt_secret(
    settings: Settings,
    *,
    ciphertext: bytes,
    dek_wrapped: bytes,
    aad: bytes,
) -> bytes:
    master = _master_key(settings)
    if len(dek_wrapped) < 1 + _NONCE_BYTES + _KEY_BYTES + 16:
        raise CredentialDecryptionError("wrapped DEK is truncated")
    if dek_wrapped[0] != _VERSION:
        raise CredentialDecryptionError("unsupported wrapped-DEK version")
    wrap_nonce = dek_wrapped[1 : 1 + _NONCE_BYTES]
    wrapped = dek_wrapped[1 + _NONCE_BYTES :]
    try:
        dek = AESGCM(master).decrypt(wrap_nonce, wrapped, b"zara:dek:v1")
    except InvalidTag as exc:
        raise CredentialDecryptionError("wrapped DEK authentication failed") from exc
    return _open(dek, ciphertext, aad)
