"""Week 1 close — /v1/profile (GET + PUT) on routing_preferences."""

from unittest.mock import AsyncMock

import httpx
import pytest
from asgi_lifespan import LifespanManager


@pytest.fixture
async def profile_app(monkeypatch):
    """Stand up the app with auth + queries.* monkeypatched.

    Returns (app, store) — `store` is a dict keyed by agent_id holding the
    fake row, so tests can assert / preset state.
    """
    store: dict[str, dict] = {}

    async def fake_authenticate(pool, request):
        from fastapi import HTTPException

        auth = request.headers.get("authorization", "")
        if not auth.lower().startswith("bearer ng_"):
            raise HTTPException(status_code=401, detail="bad token")
        return "alice"

    async def fake_get(pool, *, agent_id: str):
        if agent_id in store:
            return dict(store[agent_id])
        return {
            "agent_id": agent_id,
            "preferred_tier_overrides": None,
            "banned_models": [],
            "preferred_models": [],
            "notes": None,
            "updated_at": None,
        }

    async def fake_upsert(pool, *, agent_id: str, **kw):
        # Mimic the real upsert: existing row is replaced, fields are nullable.
        store[agent_id] = {
            "agent_id": agent_id,
            "preferred_tier_overrides": kw.get("preferred_tier_overrides"),
            "banned_models": list(kw.get("banned_models") or []),
            "preferred_models": list(kw.get("preferred_models") or []),
            "notes": kw.get("notes"),
            "updated_at": "2026-05-07T00:00:00",
        }
        return dict(store[agent_id])

    monkeypatch.setattr("app.routes.v1.authenticate", fake_authenticate)
    monkeypatch.setattr("app.routes.v1.queries.get_routing_preferences", fake_get)
    monkeypatch.setattr("app.routes.v1.queries.upsert_routing_preferences", fake_upsert)

    from app.main import create_app

    application = create_app()
    async with LifespanManager(application):
        application.state.db = AsyncMock()
        yield application, store


# --- GET --------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_profile_requires_auth(profile_app):
    app, _ = profile_app
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://nautgate.test") as c:
        resp = await c.get("/v1/profile")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_get_profile_returns_empty_defaults(profile_app):
    app, _ = profile_app
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://nautgate.test") as c:
        resp = await c.get("/v1/profile", headers={"Authorization": "Bearer ng_test"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["agent_id"] == "alice"
    assert body["banned_models"] == []
    assert body["preferred_tier_overrides"] is None


# --- PUT --------------------------------------------------------------------


@pytest.mark.asyncio
async def test_put_profile_round_trip(profile_app):
    app, store = profile_app
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://nautgate.test") as c:
        resp = await c.put(
            "/v1/profile",
            headers={"Authorization": "Bearer ng_test"},
            json={
                "banned_models": ["gpt-4o-mini"],
                "preferred_models": ["claude-haiku-4-5"],
                "preferred_tier_overrides": {"summarize": "balanced"},
                "notes": "no openai please",
            },
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["banned_models"] == ["gpt-4o-mini"]
    assert body["preferred_models"] == ["claude-haiku-4-5"]
    assert body["preferred_tier_overrides"] == {"summarize": "balanced"}
    assert body["notes"] == "no openai please"
    # GET reflects the upsert.
    async with httpx.AsyncClient(transport=transport, base_url="http://nautgate.test") as c:
        get_resp = await c.get("/v1/profile", headers={"Authorization": "Bearer ng_test"})
    assert get_resp.json()["banned_models"] == ["gpt-4o-mini"]


@pytest.mark.asyncio
async def test_put_profile_rejects_non_object_body(profile_app):
    app, _ = profile_app
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://nautgate.test") as c:
        resp = await c.put(
            "/v1/profile",
            headers={"Authorization": "Bearer ng_test"},
            json=["not", "an", "object"],
        )
    assert resp.status_code == 400


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["banned_models", "preferred_models"])
async def test_put_profile_rejects_non_string_lists(profile_app, field):
    app, _ = profile_app
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://nautgate.test") as c:
        resp = await c.put(
            "/v1/profile",
            headers={"Authorization": "Bearer ng_test"},
            json={field: [1, 2, 3]},
        )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_put_profile_ignores_unknown_fields(profile_app):
    app, store = profile_app
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://nautgate.test") as c:
        resp = await c.put(
            "/v1/profile",
            headers={"Authorization": "Bearer ng_test"},
            json={"banned_models": ["x"], "agent_id": "bob", "evil": True},
        )
    assert resp.status_code == 200
    # agent_id was taken from the bearer, NOT the body. No "bob" written.
    assert "bob" not in store
    assert "alice" in store


@pytest.mark.asyncio
async def test_put_profile_with_just_notes(profile_app):
    app, _ = profile_app
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://nautgate.test") as c:
        resp = await c.put(
            "/v1/profile",
            headers={"Authorization": "Bearer ng_test"},
            json={"notes": "only notes"},
        )
    assert resp.status_code == 200
    assert resp.json()["notes"] == "only notes"


# --- banned_models hooks into resolve_healthy ------------------------------


def test_banned_models_filter_in_resolve_healthy():
    from app.scoring import resolve_healthy

    table = {
        "fast": {
            "primary": {"provider": "openai", "model": "gpt-4o-mini"},
            "fallback": {"provider": "anthropic", "model": "claude-haiku-4-5"},
        }
    }
    # gpt-4o-mini banned → fall through to claude-haiku-4-5.
    r = resolve_healthy("fast", table, lambda *_: False, banned_models=["gpt-4o-mini"], enforce_subscription_ban=False)
    assert r.provider == "anthropic"
    assert r.model == "claude-haiku-4-5"


def test_banned_models_with_banned_fallback_returns_primary():
    """If both primary and fallback are banned, prefer primary over stranding the request."""
    from app.scoring import resolve_healthy

    table = {
        "fast": {
            "primary": {"provider": "openai", "model": "gpt-4o-mini"},
            "fallback": {"provider": "anthropic", "model": "claude-haiku-4-5"},
        }
    }
    r = resolve_healthy(
        "fast", table, lambda *_: False,
        banned_models=["gpt-4o-mini", "claude-haiku-4-5"],
        enforce_subscription_ban=False,
    )
    assert r.model == "gpt-4o-mini"  # primary returned despite ban
