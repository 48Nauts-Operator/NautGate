"""Symmetric encryption for provider credentials at rest (NAUTGATE-8).

Follows the pattern every comparable tool uses (LiteLLM, n8n): app-level
AES-256-GCM with a single master key held *outside* the database, so a leaked DB
dump alone is inert. HKDF-SHA256 derives the 256-bit key from it.

The master key is resolved, in order:

    1. NAUTGATE_MASTER_KEY        env var — set this to bring your own / rotate.
    2. NAUTGATE_MASTER_KEY_FILE   a file (default below), auto-generated on first
                                  boot so in-app provider keys work with ZERO
                                  configuration — no .env, no manual step.

The file lives in the persisted volume (next to backups), not in the DB, so the
"leaked DB dump is inert" property holds. LOSING the key (env value or the file)
means every stored provider key must be re-entered — there is no recovery, so it
travels with your backups: keep it if you migrate.

Provider keys are encrypted (we must *use* them). Issued `ng_` keys stay
argon2-hashed in api_keys (we only *verify* those) — see auth.py.
"""

from __future__ import annotations

import os
import secrets
import stat
from pathlib import Path

import structlog
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.hashes import SHA256
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

_MASTER_ENV = "NAUTGATE_MASTER_KEY"
_MASTER_FILE_ENV = "NAUTGATE_MASTER_KEY_FILE"
# Persisted volume path (the backups volume is mounted here in every shipped
# compose), so the auto-generated key survives container recreation.
_DEFAULT_MASTER_FILE = "/root/.nautgate/backups/.master-key"
_INFO = b"nautgate-provider-credentials-v1"
_NONCE_BYTES = 12  # 96-bit, the AES-GCM standard

log = structlog.get_logger()


class MasterKeyMissing(RuntimeError):
    """Raised when an encrypt/decrypt is attempted with no master key available."""


def _master_file() -> Path:
    return Path(os.environ.get(_MASTER_FILE_ENV, "").strip() or _DEFAULT_MASTER_FILE)


def _resolve_master() -> str | None:
    """The master key from the env var (highest priority) or the persisted file."""
    raw = os.environ.get(_MASTER_ENV, "").strip()
    if raw:
        return raw
    try:
        f = _master_file()
        if f.is_file():
            return f.read_text(encoding="utf-8").strip() or None
    except OSError:
        pass
    return None


def ensure_master_key() -> None:
    """Idempotent startup step: guarantee a master key exists so in-app provider
    keys work out of the box. If neither the env var nor the key file provides
    one, generate a random key and persist it (0600) to the key file. Never
    blocks startup — on a write failure it logs and carries on (encryption will
    surface a clear error only if someone then tries to store a provider key).
    """
    if _resolve_master():
        return
    key = secrets.token_urlsafe(48)
    f = _master_file()
    try:
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(key + "\n", encoding="utf-8")
        os.chmod(f, stat.S_IRUSR | stat.S_IWUSR)  # 0600
        log.warning("master_key_generated", path=str(f))
    except OSError as exc:
        log.warning("master_key_persist_failed", path=str(f), error=str(exc))


def master_key_configured() -> bool:
    return _resolve_master() is not None


def _derive_key() -> bytes:
    raw = _resolve_master()
    if not raw:
        raise MasterKeyMissing(
            f"No master key ({_MASTER_ENV} unset and {_master_file()} unwritable) "
            "— cannot encrypt/decrypt provider keys."
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
