"""/v1/cost/summary + /v1/cost/timeseries endpoints."""

from unittest.mock import AsyncMock

import httpx
import pytest
from asgi_lifespan import LifespanManager


@pytest.fixture
async def app_with_cost(monkeypatch):
    captured: dict = {}

    async def fake_authenticate(pool, request):
        from fastapi import HTTPException

        if not request.headers.get("authorization", "").lower().startswith("bearer ng_"):
            raise HTTPException(status_code=401, detail="bad token")
        return "alice"

    async def fake_summary(pool, *, agent_id, hours, project_id=None):
        captured["summary"] = {"agent_id": agent_id, "hours": hours, "project_id": project_id}
        return {
            "agent_id": agent_id,
            "window_hours": hours,
            "total_calls": 12,
            "total_cost_usd": 0.0345,
            "total_prompt_tokens": 1500,
            "total_completion_tokens": 800,
            "by_provider": [
                {
                    "key": "anthropic",
                    "cost_usd": 0.03,
                    "calls": 8,
                    "prompt_tokens": 1000,
                    "completion_tokens": 600,
                }
            ],
            "by_model": [],
            "by_tier": [
                {
                    "key": "fast",
                    "cost_usd": 0.005,
                    "calls": 4,
                    "prompt_tokens": 500,
                    "completion_tokens": 200,
                }
            ],
        }

    async def fake_timeseries(pool, *, agent_id, bucket, hours, project_id=None):
        captured["timeseries"] = {"agent_id": agent_id, "bucket": bucket, "hours": hours, "project_id": project_id}
        return {
            "agent_id": agent_id,
            "bucket": bucket,
            "window_hours": hours,
            "series": [
                {
                    "provider": "anthropic",
                    "points": [
                        {"ts": "2026-05-09T10:00:00+00:00", "cost_usd": 0.01, "calls": 3},
                        {"ts": "2026-05-09T11:00:00+00:00", "cost_usd": 0.02, "calls": 5},
                    ],
                },
            ],
        }

    monkeypatch.setattr("app.routes.v1.authenticate", fake_authenticate)
    monkeypatch.setattr("app.routes.v1.queries.get_cost_summary", fake_summary)
    monkeypatch.setattr("app.routes.v1.queries.get_cost_timeseries", fake_timeseries)

    from app.main import create_app

    application = create_app()
    async with LifespanManager(application):
        application.state.db = AsyncMock()
        yield application, captured


# --- /v1/cost/summary -----------------------------------------------------


@pytest.mark.asyncio
async def test_summary_requires_auth(app_with_cost):
    app, _ = app_with_cost
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://nautgate.test") as c:
        resp = await c.get("/v1/cost/summary")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_summary_default_24h(app_with_cost):
    app, captured = app_with_cost
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://nautgate.test") as c:
        resp = await c.get("/v1/cost/summary", headers={"Authorization": "Bearer ng_test"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["total_cost_usd"] == 0.0345
    assert body["by_provider"][0]["key"] == "anthropic"
    assert captured["summary"]["hours"] == 24


@pytest.mark.asyncio
async def test_summary_hours_param(app_with_cost):
    app, captured = app_with_cost
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://nautgate.test") as c:
        resp = await c.get(
            "/v1/cost/summary?hours=168", headers={"Authorization": "Bearer ng_test"}
        )
    assert resp.status_code == 200
    assert captured["summary"]["hours"] == 168


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["0", "-1", "721", "abc"])
async def test_summary_rejects_invalid_hours(app_with_cost, bad):
    app, _ = app_with_cost
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://nautgate.test") as c:
        resp = await c.get(
            f"/v1/cost/summary?hours={bad}", headers={"Authorization": "Bearer ng_test"}
        )
    assert resp.status_code == 400


# --- /v1/cost/timeseries --------------------------------------------------


@pytest.mark.asyncio
async def test_timeseries_default_bucket_hour(app_with_cost):
    app, captured = app_with_cost
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://nautgate.test") as c:
        resp = await c.get("/v1/cost/timeseries", headers={"Authorization": "Bearer ng_test"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["bucket"] == "hour"
    assert body["window_hours"] == 168
    assert body["series"][0]["provider"] == "anthropic"
    assert len(body["series"][0]["points"]) == 2
    assert captured["timeseries"]["bucket"] == "hour"


@pytest.mark.asyncio
async def test_timeseries_bucket_day(app_with_cost):
    app, captured = app_with_cost
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://nautgate.test") as c:
        resp = await c.get(
            "/v1/cost/timeseries?bucket=day&hours=720",
            headers={"Authorization": "Bearer ng_test"},
        )
    assert resp.status_code == 200
    assert captured["timeseries"]["bucket"] == "day"
    assert captured["timeseries"]["hours"] == 720


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["minute", "year", "MONTH", ""])
async def test_timeseries_rejects_invalid_bucket(app_with_cost, bad):
    app, _ = app_with_cost
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://nautgate.test") as c:
        resp = await c.get(
            f"/v1/cost/timeseries?bucket={bad}",
            headers={"Authorization": "Bearer ng_test"},
        )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_timeseries_rejects_invalid_hours(app_with_cost):
    app, _ = app_with_cost
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://nautgate.test") as c:
        resp = await c.get(
            "/v1/cost/timeseries?hours=999", headers={"Authorization": "Bearer ng_test"}
        )
    assert resp.status_code == 400
