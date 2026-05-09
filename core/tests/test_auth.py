"""Day 4a — argon2id auth + cached keyId → agent_id map.

Real verify path is exercised here with a fake asyncpg pool that returns the row we
seeded via ``issue_key()``. The chat-completions and streaming test suites monkeypatch
``authenticate`` separately so their fixtures don't depend on argon2id timings.
"""

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from asgi_lifespan import LifespanManager
from fastapi import HTTPException

from app import auth


class _FakePool:
    """Mimics asyncpg.Pool.acquire() context manager around fetchrow."""

    def __init__(self, row: dict | None):
        self._row = row
        self.fetchrow_calls = 0

    @asynccontextmanager
    async def acquire(self):
        async def fetchrow(_sql, _key_id):
            self.fetchrow_calls += 1
            return self._row

        conn = MagicMock()
        conn.fetchrow = fetchrow
        yield conn

    async def close(self) -> None:
        return None


def _request_with(authorization: str | None):
    """Build a minimal FastAPI Request shim — only `.headers` is used by authenticate."""
    headers = {}
    if authorization is not None:
        headers["authorization"] = authorization
    req = MagicMock()
    req.headers = headers
    return req


@pytest.fixture(autouse=True)
def _clear_auth_cache():
    auth.cache_clear()
    yield
    auth.cache_clear()


# --- issue_key shape --------------------------------------------------------


def test_issue_key_returns_matching_components():
    plaintext, key_id, key_hash = auth.issue_key()
    assert plaintext.startswith("ng_")
    parts = plaintext[3:].split("_", 1)
    assert parts[0] == key_id.hex
    assert len(parts[1]) >= 40  # urlsafe-base64 of 32 bytes ≈ 43 chars
    assert key_hash.startswith("$argon2")


def test_issue_key_is_unique():
    a = auth.issue_key()
    b = auth.issue_key()
    assert a[0] != b[0]
    assert a[1] != b[1]


# --- authenticate happy path ------------------------------------------------


@pytest.mark.asyncio
async def test_authenticate_accepts_valid_token():
    plaintext, key_id, key_hash = auth.issue_key()
    pool = _FakePool({"key_hash": key_hash, "agent_id": "alice"})

    agent_id = await auth.authenticate(pool, _request_with(f"Bearer {plaintext}"))
    assert agent_id == "alice"
    assert pool.fetchrow_calls == 1


@pytest.mark.asyncio
async def test_authenticate_cache_short_circuits_second_call():
    plaintext, _, key_hash = auth.issue_key()
    pool = _FakePool({"key_hash": key_hash, "agent_id": "alice"})

    req = _request_with(f"Bearer {plaintext}")
    a = await auth.authenticate(pool, req)
    b = await auth.authenticate(pool, req)
    assert a == b == "alice"
    assert pool.fetchrow_calls == 1, "cache hit must not re-query the DB"


# --- authenticate failure modes --------------------------------------------


@pytest.mark.asyncio
async def test_authenticate_unknown_key_returns_401():
    plaintext, _, _ = auth.issue_key()
    pool = _FakePool(None)  # row not found

    with pytest.raises(HTTPException) as exc:
        await auth.authenticate(pool, _request_with(f"Bearer {plaintext}"))
    assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_authenticate_secret_mismatch_returns_401():
    """Right key_id, wrong secret → 401, no cache write."""
    plaintext, key_id, key_hash = auth.issue_key()
    # Tamper with the secret portion: keep the hex prefix, flip the secret.
    bad = f"ng_{key_id.hex}_thisIsNotTheRealSecretXXXXXXXXXXXXXXXXXXXXXX"
    pool = _FakePool({"key_hash": key_hash, "agent_id": "alice"})

    with pytest.raises(HTTPException) as exc:
        await auth.authenticate(pool, _request_with(f"Bearer {bad}"))
    assert exc.value.status_code == 401
    # Sanity: cache stayed empty so the legitimate token still has to verify.
    assert auth._cache_get(bad, now=0.0) is None


@pytest.mark.parametrize(
    "header",
    [
        None,
        "",
        "ng_abc",  # missing Bearer
        "Bearer",
        "Bearer ",
        "Basic ng_abc",
        "Bearer not_an_ng_token",
        "Bearer ng_",
        "Bearer ng__nosecret",
        "Bearer ng_zznotahex_secret",
        "Bearer ng_abc_",  # empty secret
    ],
)
@pytest.mark.asyncio
async def test_malformed_tokens_return_401(header):
    pool = _FakePool({"key_hash": "should_not_be_read", "agent_id": "x"})
    with pytest.raises(HTTPException) as exc:
        await auth.authenticate(pool, _request_with(header))
    assert exc.value.status_code == 401
    # We must reject before issuing a DB lookup — saves an argon2id verify on garbage.
    assert pool.fetchrow_calls == 0


# --- HTTP-level: real authenticate wired through the route -----------------


@pytest.fixture
async def app_with_real_auth(monkeypatch):
    """Like chat_app but uses the REAL authenticate; only DB queries are mocked."""

    async def fake_precapture(pool, **kw):
        pass

    async def fake_write_outcome(pool, **kw):
        pass

    monkeypatch.setattr("app.db.queries.precapture", fake_precapture)
    monkeypatch.setattr("app.db.queries.write_outcome", fake_write_outcome)

    from app.main import create_app

    application = create_app()
    async with LifespanManager(application):
        # Plant a fake pool whose acquire() returns our row.
        plaintext, _key_id, key_hash = auth.issue_key()
        application.state._test_token = plaintext  # for the test to read

        application.state.db = _FakePool({"key_hash": key_hash, "agent_id": "alice"})

        nautrouter_mock = AsyncMock()
        nautrouter_mock.chat_completions.return_value = {
            "id": "x",
            "choices": [
                {"message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}
            ],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        }
        application.state.nautrouter = nautrouter_mock
        yield application


@pytest.mark.asyncio
async def test_route_accepts_valid_argon2_token(app_with_real_auth):
    app = app_with_real_auth
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://nautgate.test") as c:
        resp = await c.post(
            "/v1/chat/completions",
            headers={"Authorization": f"Bearer {app.state._test_token}"},
            json={"model": "x", "messages": [{"role": "user", "content": "hi"}]},
        )
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_route_accepts_x_api_key_header(app_with_real_auth):
    """Claude Code / Anthropic SDK send `x-api-key:`, not `Authorization:`."""
    app = app_with_real_auth
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://nautgate.test") as c:
        resp = await c.post(
            "/v1/chat/completions",
            headers={"x-api-key": app.state._test_token},
            json={"model": "x", "messages": [{"role": "user", "content": "hi"}]},
        )
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_route_rejects_garbage_bearer(app_with_real_auth):
    app = app_with_real_auth
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://nautgate.test") as c:
        resp = await c.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer ng_garbage"},
            json={"model": "x", "messages": [{"role": "user", "content": "hi"}]},
        )
    assert resp.status_code == 401
