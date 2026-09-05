"""Safeguard monitor, explanation, and append-only review API contracts."""

from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from asgi_lifespan import LifespanManager


@pytest.fixture
async def safeguard_app(monkeypatch):
    calls = {}

    async def auth(pool, request):
        return "alice"

    async def summary(pool, *, agent_id, hours):
        calls["summary"] = (agent_id, hours)
        return {
            "completed": 10,
            "inspected": 8,
            "confirmed_events": 1,
            "inspection_coverage": 0.8,
            "confirmed_rate": 0.125,
        }

    async def events(pool, *, agent_id, hours, limit):
        calls["events"] = (agent_id, hours, limit)
        return []

    async def patterns(pool, *, agent_id, hours):
        return {"models": [], "interpretation": "leads only"}

    async def explain(pool, *, agent_id, decision_id):
        calls["explain"] = (agent_id, decision_id)
        return {"decision_id": decision_id, "provider_confirmed_safeguard": True}

    async def review(pool, **kwargs):
        calls["review"] = kwargs
        return {"id": str(uuid4()), **kwargs}

    monkeypatch.setattr("app.routes.v1.authenticate", auth)
    monkeypatch.setattr("app.routes.v1.queries.get_safeguard_summary", summary)
    monkeypatch.setattr("app.routes.v1.queries.get_safeguard_events", events)
    monkeypatch.setattr("app.routes.v1.queries.get_safeguard_patterns", patterns)
    monkeypatch.setattr("app.routes.v1.queries.explain_model_choice", explain)
    monkeypatch.setattr("app.routes.v1.queries.create_safeguard_review", review)
    from app.main import create_app

    application = create_app()
    async with LifespanManager(application):
        application.state.db = AsyncMock()
        yield application, calls


@pytest.mark.asyncio
async def test_monitor_scope_and_window(safeguard_app):
    app, calls = safeguard_app
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        response = await c.get(
            "/v1/safeguards/summary?hours=24&agent_id=bob",
            headers={"Authorization": "Bearer ng_test"},
        )
    assert response.status_code == 200
    assert response.json()["inspection_coverage"] == 0.8
    assert calls["summary"] == ("bob", 24)


@pytest.mark.asyncio
async def test_explanation_and_review(safeguard_app):
    app, calls = safeguard_app
    decision_id = str(uuid4())
    headers = {"Authorization": "Bearer ng_test"}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        explanation = await c.get(
            f"/v1/safeguards/decisions/{decision_id}/explanation", headers=headers
        )
        review = await c.post(
            f"/v1/safeguards/decisions/{decision_id}/reviews",
            headers=headers,
            json={"disposition": "needs_investigation", "confidence": "medium"},
        )
    assert explanation.status_code == 200
    assert review.status_code == 201
    assert calls["review"]["agent_id"] == "alice"
    assert calls["review"]["reviewer_id"] == "alice"


@pytest.mark.asyncio
async def test_review_rejects_invalid_taxonomy(safeguard_app):
    app, _ = safeguard_app
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        response = await c.post(
            f"/v1/safeguards/decisions/{uuid4()}/reviews",
            headers={"Authorization": "Bearer ng_test"},
            json={"disposition": "anthropic_is_bad", "confidence": "certain"},
        )
    assert response.status_code == 400
