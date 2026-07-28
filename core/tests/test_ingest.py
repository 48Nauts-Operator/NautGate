"""nautproxy /v1/ingest — records a captured turn via the standard
precapture + persist_outcome path."""

from unittest.mock import AsyncMock

import httpx
import pytest
from asgi_lifespan import LifespanManager

TOKEN = "ingest-secret"

TURN = {
    "agent_id": "codex",
    "inbound_format": "openai_responses_ws",
    "provider": "chatgpt-oauth",
    "model": "gpt-5-codex",
    "messages": [{"role": "user", "content": "hi"}],
    "tools": [{"type": "function", "function": {"name": "read_file"}}],
    "response": {"model": "gpt-5-codex-2026", "usage": {"input_tokens": 12, "output_tokens": 8}},
    "status_code": 200,
    "duration_ms": 900,
}


@pytest.fixture
async def app_with_ingest(monkeypatch):
    captured: dict = {}

    async def fake_precapture(pool, **kwargs):
        captured["precapture"] = kwargs

    async def fake_persist_outcome(pool, spool, **kwargs):
        captured["outcome"] = kwargs

    monkeypatch.setattr("app.routes.v1.queries.precapture", fake_precapture)
    monkeypatch.setattr("app.routes.v1.persist_outcome", fake_persist_outcome)

    from app.main import create_app

    application = create_app()
    async with LifespanManager(application):
        application.state.db = AsyncMock()
        yield application, captured


def _client(app):
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://nautgate.test"
    )


@pytest.mark.asyncio
async def test_disabled_when_no_token(app_with_ingest):
    app, _ = app_with_ingest
    app.state.settings.nautgate_ingest_token = None
    async with _client(app) as c:
        resp = await c.post("/v1/ingest", json=TURN, headers={"X-Ingest-Token": TOKEN})
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_rejects_bad_token(app_with_ingest):
    app, _ = app_with_ingest
    app.state.settings.nautgate_ingest_token = TOKEN
    async with _client(app) as c:
        resp = await c.post("/v1/ingest", json=TURN, headers={"X-Ingest-Token": "wrong"})
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_records_turn(app_with_ingest):
    app, captured = app_with_ingest
    app.state.settings.nautgate_ingest_token = TOKEN
    async with _client(app) as c:
        resp = await c.post("/v1/ingest", json=TURN, headers={"X-Ingest-Token": TOKEN})
    assert resp.status_code == 200
    assert resp.json()["ok"] is True

    pre = captured["precapture"]
    assert pre["agent_id"] == "codex"
    assert pre["inbound_format"] == "openai_responses_ws"
    assert pre["decision_provider"] == "chatgpt-oauth"
    assert pre["model_requested"] == "gpt-5-codex"
    assert pre["messages_count"] == 1
    assert pre["tools_count"] == 1
    assert pre["prompt_body"] is not None  # captured at sensitivity "none"

    out = captured["outcome"]
    assert out["status_code"] == 200
    assert out["duration_ms"] == 900
    # attestation: served model read from the response, not the request
    assert out["actual_model"] == "gpt-5-codex-2026"
    assert out["actual_provider"] == "chatgpt-oauth"
    # usage normalized from response.usage (input/output tokens)
    assert out["prompt_tokens"] == 12
    assert out["completion_tokens"] == 8
