"""Day 4a — argon2id auth + cached keyId → agent_id map.

Token format: ``ng_<key_id_hex>_<secret>`` where:
  - key_id_hex is the api_keys row UUID written without dashes (32 hex chars)
  - secret is a urlsafe random string (43 chars from secrets.token_urlsafe(32))

The api_keys.key_hash column stores argon2id(secret). On each request we parse the
token, look up the row by id (indexed primary key), verify the secret with argon2id,
and cache (token → agent_id) for _CACHE_TTL_SEC so the next request short-circuits
the hash. Per Tech Paper §7.1 (token format) and §7.2 (cache).

This module intentionally returns the same constant 401 message on every failure so
the response surface doesn't leak which part of the token was bad.
"""

import secrets
import time
import uuid

import asyncpg
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError
from fastapi import HTTPException, Request

_PH = PasswordHasher()
_CACHE_TTL_SEC = 300.0
_CACHE: dict[str, tuple[str, float]] = {}

_BAD_TOKEN = HTTPException(status_code=401, detail="missing or invalid bearer token")


def issue_key() -> tuple[str, uuid.UUID, str]:
    """Generate a fresh API key.

    Returns (plaintext_token, key_id, key_hash). The plaintext is shown to the
    operator once and never persisted; the row is inserted with (key_id, key_hash).
    """
    key_id = uuid.uuid4()
    secret = secrets.token_urlsafe(32)
    plaintext = f"ng_{key_id.hex}_{secret}"
    key_hash = _PH.hash(secret)
    return plaintext, key_id, key_hash


def _parse_bearer(authorization: str) -> str:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise _BAD_TOKEN
    return authorization.split(" ", 1)[1].strip()


def _split_token(raw: str) -> tuple[uuid.UUID, str]:
    if not raw.startswith("ng_"):
        raise _BAD_TOKEN
    rest = raw[3:]
    sep = rest.find("_")
    if sep <= 0 or sep == len(rest) - 1:
        raise _BAD_TOKEN
    key_hex, secret = rest[:sep], rest[sep + 1 :]
    try:
        key_id = uuid.UUID(hex=key_hex)
    except ValueError:
        raise _BAD_TOKEN from None
    return key_id, secret


def _cache_get(token: str, *, now: float) -> str | None:
    entry = _CACHE.get(token)
    if entry is None:
        return None
    agent_id, expires_at = entry
    if expires_at < now:
        _CACHE.pop(token, None)
        return None
    return agent_id


def _cache_put(token: str, agent_id: str, *, now: float) -> None:
    _CACHE[token] = (agent_id, now + _CACHE_TTL_SEC)


def cache_clear() -> None:
    """Drop all cached auth entries. Tests + future key-revocation paths call this."""
    _CACHE.clear()


async def authenticate(pool: asyncpg.Pool, request: Request) -> str:
    """Verify the Authorization header and return the caller's agent_id.

    Cache hit: returns immediately without touching the DB or argon2id.
    Cache miss: looks up api_keys by id, verifies argon2id, caches the result.
    """
    auth_header = request.headers.get("authorization", "")
    raw = _parse_bearer(auth_header)

    now = time.monotonic()
    cached = _cache_get(raw, now=now)
    if cached is not None:
        return cached

    key_id, secret = _split_token(raw)

    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT key_hash, agent_id FROM nautgate.api_keys WHERE id = $1",
            key_id,
        )
    if row is None:
        raise _BAD_TOKEN

    try:
        _PH.verify(row["key_hash"], secret)
    except (VerifyMismatchError, InvalidHashError):
        raise _BAD_TOKEN from None

    agent_id = row["agent_id"]
    _cache_put(raw, agent_id, now=now)
    return agent_id
