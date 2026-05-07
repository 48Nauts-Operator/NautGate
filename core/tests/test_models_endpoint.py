"""Week 1 close — /v1/models lists tier routes from the routing table."""

from unittest.mock import AsyncMock

import httpx
import pytest
from asgi_lifespan import LifespanManager


@pytest.fixture
async def models_app(monkeypatch):
    async def fake_authenticate(pool, request):
        from fastapi import HTTPException

        if not request.headers.get("authorization", "").lower().startswith("bearer ng_"):
            raise HTTPException(status_code=401, detail="bad token")
        return "anonymous"

    monkeypatch.setattr("app.routes.v1.authenticate", fake_authenticate)

    from app.main import create_app

    application = create_app()
    async with LifespanManager(application):
        application.state.db = AsyncMock()
        application.state.routing_table = {
            "fast": {
                "primary": {"provider": "openai", "model": "gpt-4o-mini"},
                "fallback": {"provider": "anthropic", "model": "claude-haiku-4-5"},
            },
            "balanced": {
                "primary": {"provider": "anthropic", "model": "claude-haiku-4-5"},
                "fallback": {"provider": "openai", "model": "gpt-4o"},
            },
            "deep": {
                "primary": {"provider": "anthropic", "model": "claude-sonnet-4-6"},
            },
            "expert": {
                "primary": {"provider": "anthropic", "model": "claude-opus-4-7"},
            },
        }
        yield application


@pytest.mark.asyncio
async def test_models_requires_auth(models_app):
    transport = httpx.ASGITransport(app=models_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://nautgate.test") as c:
        resp = await c.get("/v1/models")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_models_returns_openai_shape(models_app):
    transport = httpx.ASGITransport(app=models_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://nautgate.test") as c:
        resp = await c.get("/v1/models", headers={"Authorization": "Bearer ng_test"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["object"] == "list"
    assert isinstance(body["data"], list)
    for m in body["data"]:
        assert m["object"] == "model"
        assert m["id"]
        assert m["owned_by"]


@pytest.mark.asyncio
async def test_models_includes_auto_first(models_app):
    transport = httpx.ASGITransport(app=models_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://nautgate.test") as c:
        resp = await c.get("/v1/models", headers={"Authorization": "Bearer ng_test"})
    body = resp.json()
    assert body["data"][0]["id"] == "auto"
    assert body["data"][0]["owned_by"] == "nautgate"
    assert set(body["data"][0]["nautgate_tiers"]) == {"fast", "balanced", "deep", "expert"}


@pytest.mark.asyncio
async def test_models_dedupes_across_tiers(models_app):
    """claude-haiku-4-5 appears as fast.fallback and balanced.primary — emit once."""
    transport = httpx.ASGITransport(app=models_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://nautgate.test") as c:
        resp = await c.get("/v1/models", headers={"Authorization": "Bearer ng_test"})
    body = resp.json()
    haikus = [m for m in body["data"] if m["id"] == "claude-haiku-4-5"]
    assert len(haikus) == 1
    assert "fast:fallback" in haikus[0]["nautgate_tiers"]
    assert "balanced:primary" in haikus[0]["nautgate_tiers"]


@pytest.mark.asyncio
async def test_models_marks_unhealthy(models_app, monkeypatch):
    """Provider health tracker → nautgate_unhealthy: true on the model entry."""
    from app.provider_health import ProviderHealthTracker

    tracker = ProviderHealthTracker(threshold=2)
    tracker.record("openai", "gpt-4o-mini", was_empty=True)
    tracker.record("openai", "gpt-4o-mini", was_empty=True)
    assert tracker.is_unhealthy("openai", "gpt-4o-mini") is True
    models_app.state.health_tracker = tracker

    transport = httpx.ASGITransport(app=models_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://nautgate.test") as c:
        resp = await c.get("/v1/models", headers={"Authorization": "Bearer ng_test"})
    body = resp.json()
    bad = next(m for m in body["data"] if m["id"] == "gpt-4o-mini")
    assert bad.get("nautgate_unhealthy") is True
    # Healthy ones don't carry the flag.
    haiku = next(m for m in body["data"] if m["id"] == "claude-haiku-4-5")
    assert "nautgate_unhealthy" not in haiku


@pytest.mark.asyncio
async def test_models_with_empty_routing_table(monkeypatch):
    """No routing table → only the synthetic auto entry."""

    async def fake_authenticate(pool, request):
        return "anonymous"

    monkeypatch.setattr("app.routes.v1.authenticate", fake_authenticate)

    from app.main import create_app

    application = create_app()
    async with LifespanManager(application):
        application.state.db = AsyncMock()
        application.state.routing_table = None

        transport = httpx.ASGITransport(app=application)
        async with httpx.AsyncClient(transport=transport, base_url="http://nautgate.test") as c:
            resp = await c.get("/v1/models", headers={"Authorization": "Bearer ng_test"})
        body = resp.json()
        assert body["data"] == [
            {
                "id": "auto",
                "object": "model",
                "owned_by": "nautgate",
                "nautgate_provider": "nautgate",
                "nautgate_tiers": [],
            }
        ]
