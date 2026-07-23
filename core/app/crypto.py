"""Symmetric encryption for provider credentials at rest (NAUTGATE-8).

Follows the pattern every comparable tool uses (LiteLLM, n8n): app-level
AES-256-GCM with a single master key supplied via an env var and held *outside*
the database, so a leaked DB dump alone is inert.

    NAUTGATE_MASTER_KEY   any sufficiently-random string. HKDF-SHA256 derives the
                          256-bit key from it. LOSING IT means every stored
                          provider key must be re-entered — there is no recovery.

Provider keys are encrypted (we must *use* them). Issued `ng_` keys stay
argon2-hashed in api_keys (we only *verify* those) — see auth.py.
"""

from __future__ import annotations

import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.hashes import SHA256
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

_MASTER_ENV = "NAUTGATE_MASTER_KEY"
_INFO = b"nautgate-provider-credentials-v1"
_NONCE_BYTES = 12  # 96-bit, the AES-GCM standard


class MasterKeyMissing(RuntimeError):
    """Raised when an encrypt/decrypt is attempted with no master key set."""


def master_key_configured() -> bool:
    return bool(os.environ.get(_MASTER_ENV, "").strip())


def _derive_key() -> bytes:
    raw = os.environ.get(_MASTER_ENV, "").strip()
    if not raw:
        raise MasterKeyMissing(
            f"{_MASTER_ENV} is not set — cannot encrypt/decrypt provider keys. "
            "Set it to a random secret and keep a backup; losing it means "
            "re-entering every provider key."
        )
    return HKDF(algorithm=SHA256(), length=32, salt=None, info=_INFO).derive(raw.encode())


def encrypt(plaintext: str) -> tuple[bytes, bytes]:
    """Return (ciphertext, nonce). Fresh random nonce per call."""
    nonce = os.urandom(_NONCE_BYTES)
    ct = AESGCM(_derive_key()).encrypt(nonce, plaintext.encode(), None)
    return ct, nonce


def decrypt(ciphertext: bytes, nonce: bytes) -> str:
    """Inverse of encrypt. Raises on a wrong key or tampered ciphertext (AEAD)."""
    return AESGCM(_derive_key()).decrypt(nonce, ciphertext, None).decode()


def last4(secret: str) -> str:
    """Display suffix — never the whole key."""
    return secret[-4:] if len(secret) >= 4 else "****"
