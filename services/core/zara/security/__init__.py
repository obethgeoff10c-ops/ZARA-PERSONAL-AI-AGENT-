"""Security primitives (credential encryption, OAuth state helpers)."""

from __future__ import annotations

from zara.security.crypto import (
    CredentialDecryptionError,
    EncryptedBundle,
    EncryptedSecret,
    credential_aad,
    decrypt_secret,
    encrypt_many,
    encrypt_secret,
    state_aad,
)

__all__ = [
    "CredentialDecryptionError",
    "EncryptedBundle",
    "EncryptedSecret",
    "credential_aad",
    "decrypt_secret",
    "encrypt_many",
    "encrypt_secret",
    "state_aad",
]
