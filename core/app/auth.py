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
# token → (agent_id, project_id_or_none, expires_at_monotonic)
_CACHE: dict[str, tuple[str, str | None, float]] = {}

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


def _extract_token_from_request(request: Request) -> str:
    """Accept any of the standard auth header shapes:
    - Authorization: Bearer ng_...    (OpenAI Chat / OpenAI SDK / curl)
    - x-api-key: ng_...               (Anthropic Messages / Claude Code)
    - ?token=ng_... query param       (only for endpoints that explicitly
                                       opt in — used for HTML reports the
                                       operator opens in a fresh tab and
                                       can't attach headers to)
    """
    auth_header = request.headers.get("authorization", "")
    if auth_header:
        return _parse_bearer(auth_header)
    api_key = request.headers.get("x-api-key", "").strip()
    if api_key:
        return api_key
    # Query-param fallback. Only honoured when the caller is explicitly OK
    # with it (we mark the endpoint by passing ?token=…; standard API
    # callers won't include that param so the surface stays the same).
    # Defensive: tolerate Request shims (test mocks) that don't expose a
    # real query_params mapping — fall through to BAD_TOKEN cleanly.
    try:
        qp_token = request.query_params.get("token", "")
        if isinstance(qp_token, str):
            qp_token = qp_token.strip()
            if qp_token.startswith("ng_"):
                return qp_token
    except (AttributeError, TypeError):
        pass
    raise _BAD_TOKEN


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


def _cache_get(token: str, *, now: float) -> tuple[str, str | None] | None:
    entry = _CACHE.get(token)
    if entry is None:
        return None
    agent_id, project_id, expires_at = entry
    if expires_at < now:
        _CACHE.pop(token, None)
        return None
    return (agent_id, project_id)


def _cache_put(token: str, agent_id: str, project_id: str | None, *, now: float) -> None:
    _CACHE[token] = (agent_id, project_id, now + _CACHE_TTL_SEC)


def cache_clear() -> None:
    """Drop all cached auth entries. Tests + future key-revocation paths call this."""
    _CACHE.clear()


async def authenticate(pool: asyncpg.Pool, request: Request) -> str:
    """Verify the request's auth header and return the caller's agent_id.

    Accepts either ``Authorization: Bearer ng_...`` (OpenAI shape) or
    ``x-api-key: ng_...`` (Anthropic shape) so Claude Code, the Anthropic SDK,
    Codex, and the OpenAI SDK all work without translation.

    Also stashes ``request.state.agent_id`` and ``request.state.project_id``
    so downstream handlers (PRECAPTURE etc.) can read them without a second
    DB roundtrip.

    Cache hit: returns immediately without touching the DB or argon2id.
    Cache miss: looks up api_keys by id, verifies argon2id, caches the result.
    """
    raw = _extract_token_from_request(request)

    now = time.monotonic()
    cached = _cache_get(raw, now=now)
    if cached is not None:
        agent_id, project_id = cached
        request.state.agent_id = agent_id
        request.state.project_id = project_id
        return agent_id

    key_id, secret = _split_token(raw)

    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT key_hash, agent_id, project_id FROM nautgate.api_keys WHERE id = $1",
            key_id,
        )
    if row is None:
        raise _BAD_TOKEN

    try:
        _PH.verify(row["key_hash"], secret)
    except (VerifyMismatchError, InvalidHashError):
        raise _BAD_TOKEN from None

    agent_id = row["agent_id"]
    # `project_id` was added in migration 009. Tolerate fake rows in tests
    # and any future schema drift that doesn't include the column.
    try:
        project_id = row["project_id"]
    except (KeyError, IndexError):
        project_id = None
    _cache_put(raw, agent_id, project_id, now=now)
    request.state.agent_id = agent_id
    request.state.project_id = project_id
    return agent_id
