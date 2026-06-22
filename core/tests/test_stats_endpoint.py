"""Week 1 close — /v1/stats aggregates per-agent route_decisions + outcomes."""

from unittest.mock import AsyncMock

import httpx
import pytest
from asgi_lifespan import LifespanManager


@pytest.fixture
async def stats_app(monkeypatch):
    captured: dict = {}

    async def fake_authenticate(pool, request):
        from fastapi import HTTPException

        if not request.headers.get("authorization", "").lower().startswith("bearer ng_"):
            raise HTTPException(status_code=401, detail="bad token")
        return "alice"

    async def fake_get_stats(pool, *, agent_id: str, hours: int):
        captured["agent_id"] = agent_id
        captured["hours"] = hours
        return {
            "agent_id": agent_id,
            "window_hours": hours,
            "requests_total": 10,
            "empty_count": 1,
            "empty_rate": 0.1,
            "latency_ms": {"avg": 250.0, "p50": 200.0, "p95": 600.0},
            "cost_usd_total": 0.123,
            "requests_by_tier": {"fast": 6, "balanced": 4},
            "requests_by_inbound_format": {"openai_chat": 7, "anthropic": 3},
        }

    monkeypatch.setattr("app.routes.v1.authenticate", fake_authenticate)
    monkeypatch.setattr("app.routes.v1.queries.get_stats", fake_get_stats)

    from app.main import create_app

    application = create_app()
    async with LifespanManager(application):
        application.state.db = AsyncMock()
        yield application, captured


@pytest.mark.asyncio
async def test_stats_requires_auth(stats_app):
    app, _ = stats_app
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://nautgate.test") as c:
        resp = await c.get("/v1/stats")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_stats_returns_aggregated_shape(stats_app):
    app, captured = stats_app
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://nautgate.test") as c:
        resp = await c.get("/v1/stats", headers={"Authorization": "Bearer ng_test"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["agent_id"] == "alice"
    assert body["window_hours"] == 24
    assert body["requests_total"] == 10
    assert body["empty_rate"] == 0.1
    assert body["latency_ms"]["p95"] == 600.0
    assert body["requests_by_tier"]["fast"] == 6
    # Verify the query-layer call carried the right args.
    assert captured["agent_id"] == "alice"
    assert captured["hours"] == 24


@pytest.mark.asyncio
async def test_stats_respects_hours_query_param(stats_app):
    app, captured = stats_app
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://nautgate.test") as c:
        resp = await c.get("/v1/stats?hours=168", headers={"Authorization": "Bearer ng_test"})
    assert resp.status_code == 200
    assert captured["hours"] == 168


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["0", "-5", "87601", "abc", "1.5"])
async def test_stats_rejects_invalid_hours(stats_app, bad):
    app, _ = stats_app
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://nautgate.test") as c:
        resp = await c.get(f"/v1/stats?hours={bad}", headers={"Authorization": "Bearer ng_test"})
    assert resp.status_code == 400
