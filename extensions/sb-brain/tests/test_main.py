"""sb-brain HTTP-level tests."""

import sys
from pathlib import Path

import httpx
import pytest
from asgi_lifespan import LifespanManager

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@pytest.fixture
async def client(monkeypatch):
    # Force no DB — the lifespan should still come up cleanly.
    monkeypatch.delenv("SB_BRAIN_DB_URL", raising=False)
    from main import app

    async with LifespanManager(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://sb-brain.test") as c:
            yield c, app


@pytest.mark.asyncio
async def test_health(client):
    c, _ = client
    resp = await c.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["db_connected"] is False  # no DSN configured in test


@pytest.mark.asyncio
async def test_before_route_returns_empty_when_no_db(client):
    c, _ = client
    resp = await c.post(
        "/v1/before_route",
        json={"agent_id": "alice", "classified_tier": "fast"},
    )
    assert resp.status_code == 200
    assert resp.json() == {}


@pytest.mark.asyncio
async def test_on_outcome_invalidates_cache(client):
    c, app = client
    # Plant a cache entry.
    app.state.cache.put("alice", {"x": 1})
    resp = await c.post("/v1/on_outcome", json={"agent_id": "alice"})
    assert resp.status_code == 200
    assert app.state.cache.get("alice") is None


@pytest.mark.asyncio
async def test_on_outcome_no_agent_id_is_ok(client):
    c, _ = client
    resp = await c.post("/v1/on_outcome", json={})
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_log_endpoint(client):
    c, _ = client
    resp = await c.post("/v1/log", json={"decision_id": "x"})
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_rejects_non_object(client):
    c, _ = client
    resp = await c.post("/v1/before_route", json=["bad"])
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_rejects_invalid_json(client):
    c, _ = client
    resp = await c.post(
        "/v1/before_route",
        content=b"not-json",
        headers={"Content-Type": "application/json"},
    )
    assert resp.status_code == 400
