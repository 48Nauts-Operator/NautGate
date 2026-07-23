"""Week 3 — precedence ladder per Tech Paper §2.5.

Order from highest priority:
  2. X-Naut-Model header (per-request hard override)
  5. Brain `before_route.override_model` (brain hard override)
  6. Brain demoted_models (extends banned for auto)
  7. Brain preferred_tier (tier nudge)
  8. Score-based pick (default)

Levels 3 (api_keys.override_model) and 4 (routing_preferences.preferred_models)
are deferred for v1.
"""

from unittest.mock import AsyncMock

import httpx
import pytest
from asgi_lifespan import LifespanManager


def _routing_table():
    # Test fixture uses neutral non-subscription models so the
    # subscription-owned guard in resolve_healthy doesn't reject them.
    # Real routing.yaml uses similar Kimi/DeepSeek/Gemini choices.
    return {
        "fast": {
            "primary": {"provider": "openrouter", "model": "openrouter/deepseek/deepseek-v4-flash"},
            "fallback": {
                "provider": "openrouter",
                "model": "openrouter/moonshotai/kimi-k2-thinking",
            },
        },
        "balanced": {
            "primary": {"provider": "openrouter", "model": "openrouter/moonshotai/kimi-k2-thinking"}
        },
        "deep": {
            "primary": {"provider": "openrouter", "model": "openrouter/deepseek/deepseek-v4-pro"}
        },
        "expert": {
            "primary": {"provider": "openrouter", "model": "openrouter/moonshotai/kimi-k2.6"}
        },
    }


@pytest.fixture
async def app_with_brain(monkeypatch):
    """App where the plugin registry is replaced with a stub returning canned hints."""
    precapture: list[dict] = []

    async def fake_precapture(pool, **kw):
        precapture.append(kw)

    async def fake_write_outcome(pool, **kw):
        pass

    async def fake_upsert_health(pool, **kw):
        pass

    async def fake_authenticate(pool, request):
        from fastapi import HTTPException

        if not request.headers.get("authorization", "").lower().startswith("bearer ng_"):
            raise HTTPException(status_code=401, detail="bad token")
        return "alice"

    async def fake_get_prefs(pool, *, agent_id):
        return {
            "agent_id": agent_id,
            "preferred_tier_overrides": None,
            "banned_models": [],
            "preferred_models": [],
            "notes": None,
            "updated_at": None,
        }

    monkeypatch.setattr("app.db.queries.precapture", fake_precapture)
    monkeypatch.setattr("app.db.queries.write_outcome", fake_write_outcome)
    monkeypatch.setattr("app.routes.v1.upsert_health", fake_upsert_health)
    monkeypatch.setattr("app.routes.v1.authenticate", fake_authenticate)
    monkeypatch.setattr("app.routes.v1.queries.get_routing_preferences", fake_get_prefs)

    from app.main import create_app

    application = create_app()
    async with LifespanManager(application):
        application.state.db = AsyncMock()
        nr_mock = AsyncMock()
        nr_mock.chat_completions.return_value = {
            "choices": [
                {"message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}
            ],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        }
        application.state.nautrouter = nr_mock
        application.state.routing_table = _routing_table()

        # Stub the plugin registry — every test rebinds .agg to set the canned response.
        class _StubPlugins:
            def __init__(self):
                self.is_empty = False
                self.agg = {
                    "brain_hints": {},
                    "banned_models": [],
                    "preferred_tier": None,
                    "override_model": None,
                    "demoted_models": [],
                    "promoted_models": [],
                }

            def subscribers(self, hook):
                return [object()] if hook == "before_route" else []

            async def call_before_route(self, payload):
                return self.agg

            def dispatch_on_request(self, payload):
                pass

            def dispatch_on_response(self, payload):
                pass

            def dispatch_after_route(self, payload):
                pass

            def dispatch_on_outcome(self, payload):
                pass

            async def aclose(self):
                pass

        application.state.plugins = _StubPlugins()
        yield application, {"precapture": precapture, "nautrouter": nr_mock}


# --- Level 2: X-Naut-Model header ----------------------------------------


@pytest.mark.asyncio
async def test_x_naut_model_header_hard_overrides_auto(app_with_brain):
    app, calls = app_with_brain
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://nautgate.test") as c:
        resp = await c.post(
            "/v1/chat/completions",
            headers={
                "Authorization": "Bearer ng_test",
                "X-Naut-Model": "claude-opus-4-7",
            },
            json={"model": "auto", "messages": [{"role": "user", "content": "hi"}]},
        )
    assert resp.status_code == 200
    pc = calls["precapture"][0]
    assert pc["decision_provider"] == "override"
    assert pc["decision_model"] == "claude-opus-4-7"
    assert "header" in pc["decision_reason"]
    forwarded = calls["nautrouter"].chat_completions.call_args.args[0]
    assert forwarded["model"] == "claude-opus-4-7"


@pytest.mark.asyncio
async def test_x_naut_model_header_overrides_brain(app_with_brain):
    """Header (level 2) outranks brain.override_model (level 5)."""
    app, calls = app_with_brain
    app.state.plugins.agg["override_model"] = "claude-haiku-4-5"
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://nautgate.test") as c:
        resp = await c.post(
            "/v1/chat/completions",
            headers={
                "Authorization": "Bearer ng_test",
                "X-Naut-Model": "gpt-4o",
            },
            json={"model": "auto", "messages": [{"role": "user", "content": "hi"}]},
        )
    assert resp.status_code == 200
    assert calls["precapture"][0]["decision_model"] == "gpt-4o"
    assert "header" in calls["precapture"][0]["decision_reason"]


# --- Level 5: brain.override_model ---------------------------------------


@pytest.mark.asyncio
async def test_brain_override_wins_when_no_header(app_with_brain):
    app, calls = app_with_brain
    app.state.plugins.agg["override_model"] = "claude-opus-4-7"
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://nautgate.test") as c:
        resp = await c.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer ng_test"},
            json={"model": "auto", "messages": [{"role": "user", "content": "hi"}]},
        )
    assert resp.status_code == 200
    pc = calls["precapture"][0]
    assert pc["decision_model"] == "claude-opus-4-7"
    assert "brain" in pc["decision_reason"]


# --- Level 6: brain.demoted_models extends banned -------------------------


@pytest.mark.asyncio
async def test_brain_demoted_extends_banned(app_with_brain):
    """Brain demoting fast.primary forces fallback to fast.fallback."""
    app, calls = app_with_brain
    app.state.plugins.agg["demoted_models"] = [
        "openrouter/deepseek/deepseek-v4-flash"
    ]  # fast.primary
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://nautgate.test") as c:
        resp = await c.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer ng_test"},
            json={"model": "auto", "messages": [{"role": "user", "content": "hi"}]},
        )
    assert resp.status_code == 200
    pc = calls["precapture"][0]
    assert pc["decision_model"] == "openrouter/moonshotai/kimi-k2-thinking"  # fallback
    assert pc["decision_provider"] == "openrouter"


# --- Level 7: brain.preferred_tier nudge ----------------------------------


@pytest.mark.asyncio
async def test_preferred_tier_nudges_routing(app_with_brain):
    """A short prompt scores 'fast'; brain says 'deep' → routes to deep tier."""
    app, calls = app_with_brain
    app.state.plugins.agg["preferred_tier"] = "deep"
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://nautgate.test") as c:
        resp = await c.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer ng_test"},
            json={"model": "auto", "messages": [{"role": "user", "content": "hi"}]},
        )
    assert resp.status_code == 200
    pc = calls["precapture"][0]
    assert pc["classified_tier"] == "deep"
    assert pc["decision_model"] == "openrouter/deepseek/deepseek-v4-pro"  # deep.primary


# --- Level 8: default score-based pick (no overrides) ---------------------


@pytest.mark.asyncio
async def test_no_overrides_uses_score(app_with_brain):
    """No header, no brain overrides → straight scored auto routing."""
    app, calls = app_with_brain
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://nautgate.test") as c:
        resp = await c.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer ng_test"},
            json={"model": "auto", "messages": [{"role": "user", "content": "hi"}]},
        )
    assert resp.status_code == 200
    pc = calls["precapture"][0]
    assert pc["classified_tier"] == "fast"
    assert pc["decision_model"] == "openrouter/deepseek/deepseek-v4-flash"  # fast.primary


# --- Header override even works for non-auto models -----------------------


@pytest.mark.asyncio
async def test_header_overrides_explicit_model(app_with_brain):
    """X-Naut-Model overrides even when caller passed an explicit model."""
    app, calls = app_with_brain
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://nautgate.test") as c:
        resp = await c.post(
            "/v1/chat/completions",
            headers={
                "Authorization": "Bearer ng_test",
                "X-Naut-Model": "claude-opus-4-7",
            },
            json={
                "model": "claude-haiku-4-5",  # explicit
                "messages": [{"role": "user", "content": "hi"}],
            },
        )
    assert resp.status_code == 200
    pc = calls["precapture"][0]
    assert pc["decision_model"] == "claude-opus-4-7"
    assert pc["decision_provider"] == "override"
