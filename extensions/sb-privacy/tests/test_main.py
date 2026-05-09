"""sb-privacy HTTP-level tests."""

import sys
from pathlib import Path

import httpx
import pytest
from asgi_lifespan import LifespanManager

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@pytest.fixture
async def client(monkeypatch):
    monkeypatch.delenv("SB_PRIVACY_DB_URL", raising=False)
    from main import app

    async with LifespanManager(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://sb-privacy.test") as c:
            yield c, app


@pytest.mark.asyncio
async def test_health(client):
    c, _ = client
    resp = await c.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["allowlist_models"] >= 0


@pytest.mark.asyncio
async def test_before_route_clean_returns_empty(client):
    c, _ = client
    resp = await c.post(
        "/v1/before_route",
        json={"agent_id": "alice", "classified_sensitivity": "none"},
    )
    assert resp.status_code == 200
    assert resp.json() == {}


@pytest.mark.asyncio
async def test_before_route_pii_returns_promoted_models(client):
    c, app = client
    resp = await c.post(
        "/v1/before_route",
        json={
            "agent_id": "alice",
            "classified_sensitivity": "pii",
            "classified_signals": [{"rule_id": "email"}],
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert "promoted_models" in body
    # promoted_models matches the bundled allowlist.
    assert set(body["promoted_models"]) == set(app.state.allowlist["models"])
    assert "reason" in body["brain_hints"]


@pytest.mark.asyncio
async def test_before_route_secret_returns_promoted_models(client):
    c, _ = client
    resp = await c.post(
        "/v1/before_route",
        json={"agent_id": "alice", "classified_sensitivity": "secret"},
    )
    body = resp.json()
    assert "promoted_models" in body
    assert "secret" in body["brain_hints"]["reason"]


@pytest.mark.asyncio
async def test_on_request_clean_skips_log(client):
    c, _ = client
    resp = await c.post(
        "/v1/on_request",
        json={"agent_id": "alice", "classified_sensitivity": "none"},
    )
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "logged": False}


@pytest.mark.asyncio
async def test_on_request_pii_no_db_returns_no_db(client):
    c, _ = client
    resp = await c.post(
        "/v1/on_request",
        json={
            "agent_id": "alice",
            "classified_sensitivity": "pii",
            "decision_id": "abc-123",
            "prompt_excerpt": "send my password",
        },
    )
    body = resp.json()
    assert body["ok"] is True
    assert body["logged"] is False
    assert body["reason"] == "no_db"


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
