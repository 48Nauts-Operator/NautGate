from unittest.mock import AsyncMock

import httpx
import pytest
from asgi_lifespan import LifespanManager


@pytest.fixture
async def chat_app(monkeypatch):
    """App with DB queries mocked and a fake NautRouterClient injected.

    Returns (app, calls) where calls is a dict with two lists:
        precapture: kwargs for each precapture() invocation
        outcome:    kwargs for each write_outcome() invocation
    """
    precapture_calls: list[dict] = []
    outcome_calls: list[dict] = []

    async def fake_precapture(pool, **kw):
        precapture_calls.append(kw)

    async def fake_write_outcome(pool, **kw):
        outcome_calls.append(kw)

    async def fake_authenticate(pool, request):
        from fastapi import HTTPException

        auth = request.headers.get("authorization", "")
        if not auth.lower().startswith("bearer ng_"):
            raise HTTPException(status_code=401, detail="missing or invalid bearer token")
        return "anonymous"

    monkeypatch.setattr("app.db.queries.precapture", fake_precapture)
    monkeypatch.setattr("app.db.queries.write_outcome", fake_write_outcome)
    monkeypatch.setattr("app.routes.v1.authenticate", fake_authenticate)

    from app.main import create_app

    application = create_app()
    async with LifespanManager(application):
        # Inject post-lifespan: a mock DB pool and a mock NautRouter client.
        application.state.db = AsyncMock()
        mock = AsyncMock()
        application.state.nautrouter = mock
        yield application, {"precapture": precapture_calls, "outcome": outcome_calls, "mock": mock}


@pytest.fixture
async def chat_client(chat_app):
    app, calls = chat_app
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://nautgate.test") as c:
        yield c, calls


# --- Auth / validation -------------------------------------------------------


@pytest.mark.asyncio
async def test_missing_bearer_returns_401(chat_client):
    c, _ = chat_client
    resp = await c.post("/v1/chat/completions", json={"model": "x", "messages": []})
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_invalid_json_returns_400(chat_client):
    c, _ = chat_client
    resp = await c.post(
        "/v1/chat/completions",
        headers={"Authorization": "Bearer ng_test", "Content-Type": "application/json"},
        content=b"not-json",
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_missing_model_returns_400(chat_client):
    c, _ = chat_client
    resp = await c.post(
        "/v1/chat/completions",
        headers={"Authorization": "Bearer ng_test"},
        json={"messages": [{"role": "user", "content": "hi"}]},
    )
    assert resp.status_code == 400


# Streaming behavior is exercised in tests/test_streaming.py.


# --- Happy path -------------------------------------------------------------


@pytest.mark.asyncio
async def test_round_trip_returns_response_and_writes_rows(chat_client):
    c, calls = chat_client

    canned = {
        "id": "chatcmpl-abc",
        "choices": [
            {"index": 0, "message": {"role": "assistant", "content": "hi"}, "finish_reason": "stop"}
        ],
        "usage": {"prompt_tokens": 12, "completion_tokens": 3, "total_tokens": 15},
    }
    calls["mock"].chat_completions.return_value = canned

    resp = await c.post(
        "/v1/chat/completions",
        headers={"Authorization": "Bearer ng_test"},
        json={"model": "claude-haiku-4.5", "messages": [{"role": "user", "content": "say hi"}]},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body == canned
    assert resp.headers.get("x-nautgate-decision-id")
    assert resp.headers.get("x-nautgate-latency-ms")
    assert resp.headers.get("x-nautgate-provider") == "passthrough"
    assert resp.headers.get("x-nautgate-model") == "claude-haiku-4.5"
    assert resp.headers.get("x-nautgate-was-empty") == "false"

    # PRECAPTURE row was written before forward.
    assert len(calls["precapture"]) == 1
    pc = calls["precapture"][0]
    assert pc["agent_id"] == "anonymous"
    assert pc["inbound_format"] == "openai_chat"
    assert pc["model_requested"] == "claude-haiku-4.5"
    assert pc["classified_tier"] == "UNCLASSIFIED"
    assert pc["prompt_excerpt"] == "say hi"

    # Outcome row was written after forward.
    assert len(calls["outcome"]) == 1
    oc = calls["outcome"][0]
    assert oc["decision_id"] == pc["decision_id"]
    assert oc["status_code"] == 200
    assert oc["prompt_tokens"] == 12
    assert oc["completion_tokens"] == 3
    assert oc["was_empty"] is False


# --- was_empty (Tongyi failure mode) ----------------------------------------


@pytest.mark.asyncio
async def test_was_empty_when_completion_tokens_but_no_content(chat_client):
    c, calls = chat_client

    calls["mock"].chat_completions.return_value = {
        "id": "x",
        "choices": [{"message": {"role": "assistant", "content": ""}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 100, "completion_tokens": 50},
    }

    resp = await c.post(
        "/v1/chat/completions",
        headers={"Authorization": "Bearer ng_test"},
        json={"model": "x", "messages": [{"role": "user", "content": "hi"}]},
    )

    assert resp.status_code == 200
    assert resp.headers["x-nautgate-was-empty"] == "true"
    assert calls["outcome"][0]["was_empty"] is True


@pytest.mark.asyncio
async def test_not_empty_when_content_present(chat_client):
    c, calls = chat_client

    calls["mock"].chat_completions.return_value = {
        "choices": [{"message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 5, "completion_tokens": 1},
    }

    resp = await c.post(
        "/v1/chat/completions",
        headers={"Authorization": "Bearer ng_test"},
        json={"model": "x", "messages": [{"role": "user", "content": "hi"}]},
    )

    assert resp.status_code == 200
    assert resp.headers["x-nautgate-was-empty"] == "false"
    assert calls["outcome"][0]["was_empty"] is False


# --- Upstream failure -------------------------------------------------------


@pytest.mark.asyncio
async def test_upstream_failure_returns_502_and_logs_outcome(chat_client):
    c, calls = chat_client

    calls["mock"].chat_completions.side_effect = httpx.ConnectError("connection refused")

    resp = await c.post(
        "/v1/chat/completions",
        headers={"Authorization": "Bearer ng_test"},
        json={"model": "x", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert resp.status_code == 502
    # PRECAPTURE landed even though upstream failed.
    assert len(calls["precapture"]) == 1
    # Outcome row was still written (to record the failure).
    assert len(calls["outcome"]) == 1
    assert calls["outcome"][0]["status_code"] == 502
