"""Week 4 — /v1/decisions/recent endpoint."""

from unittest.mock import AsyncMock

import httpx
import pytest
from asgi_lifespan import LifespanManager


@pytest.fixture
async def app_with_decisions(monkeypatch):
    captured: dict = {}

    async def fake_authenticate(pool, request):
        from fastapi import HTTPException

        if not request.headers.get("authorization", "").lower().startswith("bearer ng_"):
            raise HTTPException(status_code=401, detail="bad token")
        return "alice"

    async def fake_get_recent_decisions(pool, *, agent_id, limit, session_id=None):
        captured["agent_id"] = agent_id
        captured["limit"] = limit
        captured["session_id"] = session_id
        return [
            {
                "decision_id": "abc-1",
                "ts": "2026-05-09T12:00:00+00:00",
                "inbound_format": "openai_chat",
                "model_requested": "auto",
                "tier": "fast",
                "score": 0.05,
                "sensitivity": "none",
                "provider": "openai",
                "model": "gpt-4o-mini",
                "reason": "auto:fast->openai/gpt-4o-mini",
                "status_code": 200,
                "duration_ms": 250,
                "first_byte_ms": None,
                "prompt_tokens": 10,
                "completion_tokens": 20,
                "was_empty": False,
                "was_truncated": False,
                "client_disconnected": False,
            }
        ]

    monkeypatch.setattr("app.routes.v1.authenticate", fake_authenticate)
    monkeypatch.setattr("app.routes.v1.queries.get_recent_decisions", fake_get_recent_decisions)

    from app.main import create_app

    application = create_app()
    async with LifespanManager(application):
        application.state.db = AsyncMock()
        yield application, captured


@pytest.mark.asyncio
async def test_requires_auth(app_with_decisions):
    app, _ = app_with_decisions
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://nautgate.test") as c:
        resp = await c.get("/v1/decisions/recent")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_returns_data_with_default_limit(app_with_decisions):
    app, captured = app_with_decisions
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://nautgate.test") as c:
        resp = await c.get("/v1/decisions/recent", headers={"Authorization": "Bearer ng_test"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["agent_id"] == "alice"
    assert body["limit"] == 50
    assert isinstance(body["data"], list)
    assert body["data"][0]["decision_id"] == "abc-1"
    assert captured["limit"] == 50


@pytest.mark.asyncio
async def test_limit_query_param(app_with_decisions):
    app, captured = app_with_decisions
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://nautgate.test") as c:
        resp = await c.get(
            "/v1/decisions/recent?limit=10", headers={"Authorization": "Bearer ng_test"}
        )
    assert resp.status_code == 200
    assert captured["limit"] == 10


@pytest.mark.asyncio
async def test_session_id_scopes_recent_decisions(app_with_decisions):
    app, captured = app_with_decisions
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://nautgate.test") as c:
        resp = await c.get(
            "/v1/decisions/recent?session_id=abc123",
            headers={"Authorization": "Bearer ng_test"},
        )
    assert resp.status_code == 200
    assert resp.json()["session_id"] == "abc123"
    assert captured["session_id"] == "abc123"


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["0", "-1", "201", "abc", ""])
async def test_rejects_invalid_limit(app_with_decisions, bad):
    app, _ = app_with_decisions
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://nautgate.test") as c:
        resp = await c.get(
            f"/v1/decisions/recent?limit={bad}",
            headers={"Authorization": "Bearer ng_test"},
        )
    assert resp.status_code == 400
