from unittest.mock import AsyncMock

import httpx
import pytest
from asgi_lifespan import LifespanManager


@pytest.fixture
async def chat_app(monkeypatch, empty_db_pool):
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

    async def fake_upsert_health(pool, **kw):
        pass

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
    monkeypatch.setattr("app.routes.v1.authenticate", fake_authenticate)
    monkeypatch.setattr("app.routes.v1.upsert_health", fake_upsert_health)
    monkeypatch.setattr("app.routes.v1.queries.get_routing_preferences", fake_get_prefs)

    from app.main import create_app

    application = create_app()
    async with LifespanManager(application):
        # Inject post-lifespan: a mock DB pool and a mock NautRouter client.
        application.state.db = empty_db_pool
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
    assert resp.headers.get("x-nautgate-receipt-id")
    assert resp.headers.get("x-nautgate-evidence-status") == "pending"
    assert resp.headers.get("x-nautgate-latency-ms")
    assert resp.headers.get("x-nautgate-provider") == "passthrough"
    assert resp.headers.get("x-nautgate-model") == "claude-haiku-4.5"
    assert resp.headers.get("x-nautgate-requested-model") == "claude-haiku-4.5"
    assert resp.headers.get("x-nautgate-substituted") == "false"
    assert resp.headers.get("x-nautgate-was-empty") == "false"

    # PRECAPTURE row was written before forward.
    assert len(calls["precapture"]) == 1
    pc = calls["precapture"][0]
    assert pc["agent_id"] == "anonymous"
    assert pc["inbound_format"] == "openai_chat"
    assert pc["model_requested"] == "claude-haiku-4.5"
    # Day 5a: short "say hi" → fast tier, low score.
    assert pc["classified_tier"] == "fast"
    assert pc["classified_score"] is not None and pc["classified_score"] < 0.15
    assert pc["classified_sensitivity"] == "none"  # Day 4b: clean prompt
    assert pc["classified_signals"] == []
    assert pc["prompt_excerpt"] == "say hi"
    # Day 4c: none → full prompt body stored as JSON.
    assert pc["prompt_body"] is not None
    assert "say hi" in pc["prompt_body"]
    # Day 5b: explicit model → decision_provider stays "passthrough".
    assert pc["decision_provider"] == "passthrough"
    assert pc["decision_model"] == "claude-haiku-4.5"
    assert pc["decision_reason"] == "explicit:claude-haiku-4.5"

    # Outcome row was written after forward.
    assert len(calls["outcome"]) == 1
    oc = calls["outcome"][0]
    assert oc["decision_id"] == pc["decision_id"]
    assert oc["status_code"] == 200
    assert oc["prompt_tokens"] == 12
    assert oc["completion_tokens"] == 3
    assert oc["was_empty"] is False
    assert oc["evidence"]["receipt_id"] == resp.headers["x-nautgate-receipt-id"]


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


@pytest.mark.asyncio
async def test_provider_reported_model_substitution_is_returned_to_caller(chat_client):
    c, calls = chat_client
    calls["mock"].chat_completions.return_value = {
        "model": "claude-haiku-4-5",
        "choices": [{"message": {"role": "assistant", "content": "ok"}}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1},
    }

    resp = await c.post(
        "/v1/chat/completions",
        headers={"Authorization": "Bearer ng_test"},
        json={"model": "claude-opus-5", "messages": [{"role": "user", "content": "hi"}]},
    )

    assert resp.status_code == 200
    assert resp.headers["x-nautgate-requested-model"] == "claude-opus-5"
    assert resp.headers["x-nautgate-observed-model"] == "claude-haiku-4-5"
    assert resp.headers["x-nautgate-substituted"] == "true"
    assert resp.headers["x-nautgate-decision-id"]
    assert resp.headers["x-nautgate-receipt-id"]
    assert resp.headers["x-nautgate-evidence-status"] == "pending"


@pytest.mark.asyncio
async def test_upstream_failure_returns_decision_receipt_headers(chat_client):
    c, calls = chat_client
    response = httpx.Response(
        400,
        json={"error": {"message": "model is unavailable"}},
        request=httpx.Request("POST", "http://nautrouter.test/v1/chat/completions"),
    )
    calls["mock"].chat_completions.side_effect = httpx.HTTPStatusError(
        "bad request", request=response.request, response=response
    )

    resp = await c.post(
        "/v1/chat/completions",
        headers={"Authorization": "Bearer ng_test"},
        json={"model": "claude-opus-5", "messages": [{"role": "user", "content": "hi"}]},
    )

    assert resp.status_code == 502
    assert resp.headers["x-nautgate-decision-id"]
    assert resp.headers["x-nautgate-requested-model"] == "claude-opus-5"
    assert resp.headers["x-nautgate-upstream-status"] == "400"
    assert "model is unavailable" in resp.json()["detail"]


# --- Day 5b: model:auto routing --------------------------------------------


@pytest.fixture
def routing_table():
    return {
        "fast": {
            "primary": {"provider": "openai", "model": "gpt-4o-mini"},
            "fallback": {"provider": "anthropic", "model": "claude-haiku-4-5"},
        },
        "balanced": {"primary": {"provider": "anthropic", "model": "claude-haiku-4-5"}},
        "deep": {"primary": {"provider": "anthropic", "model": "claude-sonnet-4-6"}},
        "expert": {"primary": {"provider": "anthropic", "model": "claude-opus-4-7"}},
    }


@pytest.mark.asyncio
async def test_auto_routes_short_prompt_to_fast(chat_app, routing_table):
    app, calls = chat_app
    app.state.routing_table = routing_table

    calls["mock"].chat_completions.return_value = {
        "choices": [{"message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1},
    }

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://nautgate.test") as c:
        resp = await c.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer ng_test"},
            json={"model": "auto", "messages": [{"role": "user", "content": "hi"}]},
        )
    assert resp.status_code == 200
    assert resp.headers["x-nautgate-tier"] == "fast"
    assert resp.headers["x-nautgate-provider"] == "openai"
    assert resp.headers["x-nautgate-model"] == "gpt-4o-mini"

    pc = calls["precapture"][0]
    assert pc["classified_tier"] == "fast"
    assert pc["model_requested"] == "auto"
    assert pc["decision_provider"] == "openai"
    assert pc["decision_model"] == "gpt-4o-mini"
    assert "auto:fast" in pc["decision_reason"]
    # NautRouter received the rewritten model.
    forwarded = calls["mock"].chat_completions.call_args.args[0]
    assert forwarded["model"] == "gpt-4o-mini"


@pytest.mark.asyncio
async def test_auto_returns_503_when_routing_table_missing(chat_app):
    app, calls = chat_app
    app.state.routing_table = None

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://nautgate.test") as c:
        resp = await c.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer ng_test"},
            json={"model": "auto", "messages": [{"role": "user", "content": "hi"}]},
        )
    assert resp.status_code == 503


@pytest.mark.asyncio
async def test_explicit_model_does_not_invoke_routing(chat_app, routing_table):
    """A request with an explicit model passes through verbatim."""
    app, calls = chat_app
    app.state.routing_table = routing_table

    calls["mock"].chat_completions.return_value = {
        "choices": [{"message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1},
    }

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://nautgate.test") as c:
        resp = await c.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer ng_test"},
            json={
                "model": "my-pinned-model",
                "messages": [{"role": "user", "content": "hi"}],
            },
        )
    assert resp.status_code == 200
    assert resp.headers["x-nautgate-provider"] == "passthrough"
    assert resp.headers["x-nautgate-model"] == "my-pinned-model"
    forwarded = calls["mock"].chat_completions.call_args.args[0]
    assert forwarded["model"] == "my-pinned-model"


@pytest.mark.asyncio
async def test_confidential_policy_overrides_explicit_cloud_model(chat_app, monkeypatch):
    app, calls = chat_app

    async def confidential_settings(_pool):
        return {
            "confidentiality_routing": {
                "enabled": True,
                "local_model": "lmstudio/qwen3-local",
                "route_pii": True,
                "route_secret": True,
                "bowden_enabled": True,
            }
        }

    monkeypatch.setattr("app.app_config.get_settings", confidential_settings)
    # A local-only route must be allowed to probe its own recovery. Refusing
    # before the call would permanently latch confidential traffic at 503.
    for _ in range(3):
        app.state.health_tracker.record("lmstudio", "lmstudio/qwen3-local", was_empty=True)
    assert app.state.health_tracker.is_unhealthy("lmstudio", "lmstudio/qwen3-local")
    calls["mock"].chat_completions.return_value = {
        "choices": [{"message": {"role": "assistant", "content": "ok"}}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1},
    }

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://nautgate.test") as c:
        resp = await c.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer ng_test"},
            json={
                "model": "openrouter/cloud-model",
                "messages": [{"role": "user", "content": "AHV 756.9217.0769.85"}],
            },
        )

    assert resp.status_code == 200
    assert resp.headers["x-nautgate-confidentiality"] == "pii"
    assert resp.headers["x-nautgate-data-boundary"] == "local"
    assert resp.headers["x-nautgate-provider"] == "lmstudio"
    assert resp.headers["x-nautgate-model"] == "lmstudio/qwen3-local"
    forwarded = calls["mock"].chat_completions.call_args.args[0]
    assert forwarded["model"] == "lmstudio/qwen3-local"
    pc = calls["precapture"][0]
    assert pc["decision_provider"] == "lmstudio"
    assert "confidentiality:pii:local_only" in pc["decision_reason"]
    assert "756.9217.0769.85" not in pc["prompt_body"]
    assert "756.9217.0769.85" not in (pc["prompt_excerpt"] or "")
    assert all("756.9217.0769.85" not in repr(signal) for signal in pc["classified_signals"])


@pytest.mark.asyncio
async def test_confidential_policy_ignores_static_tool_schema_for_routing(chat_app, monkeypatch):
    app, calls = chat_app

    async def confidential_settings(_pool):
        return {
            "confidentiality_routing": {
                "enabled": True,
                "local_model": "lmstudio/qwen3-local",
                "route_pii": True,
                "route_secret": True,
                "bowden_enabled": True,
            }
        }

    monkeypatch.setattr("app.app_config.get_settings", confidential_settings)
    calls["mock"].chat_completions.return_value = {
        "choices": [{"message": {"role": "assistant", "content": "ok"}}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1},
    }
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://nautgate.test") as c:
        resp = await c.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer ng_test"},
            json={
                "model": "cloud-model",
                "messages": [{"role": "user", "content": "Use the account tool"}],
                "tools": [
                    {
                        "type": "function",
                        "function": {
                            "name": "account_lookup",
                            "description": "Lookup AHV 756.9217.0769.85",
                            "parameters": {"type": "object"},
                        },
                    }
                ],
            },
        )

    assert resp.status_code == 200
    assert resp.headers["x-nautgate-data-boundary"] == "standard"
    pc = calls["precapture"][0]
    assert pc["classified_sensitivity"] == "none"
    assert pc["decision_provider"] == "passthrough"
    assert "756.9217.0769.85" in pc["tools_body"]


@pytest.mark.asyncio
async def test_confidential_policy_missing_model_fails_closed(chat_app, monkeypatch):
    app, calls = chat_app

    async def incomplete_settings(_pool):
        return {
            "confidentiality_routing": {
                "enabled": True,
                "local_model": "",
                "route_secret": True,
                "bowden_enabled": False,
            }
        }

    monkeypatch.setattr("app.app_config.get_settings", incomplete_settings)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://nautgate.test") as c:
        resp = await c.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer ng_test"},
            json={
                "model": "cloud-model",
                "messages": [
                    {
                        "role": "user",
                        "content": "token ghp_aBcDeFgHiJkLmNoPqRsTuVwXyZ0123456789ab",
                    }
                ],
            },
        )

    assert resp.status_code == 503
    assert resp.json()["detail"]["error"] == "confidential_route_unavailable"
    calls["mock"].chat_completions.assert_not_awaited()


@pytest.mark.asyncio
async def test_invalid_confidentiality_header_returns_400(chat_client):
    c, calls = chat_client
    resp = await c.post(
        "/v1/chat/completions",
        headers={
            "Authorization": "Bearer ng_test",
            "X-NautGate-Confidentiality": "maybe",
        },
        json={"model": "x", "messages": [{"role": "user", "content": "hello"}]},
    )
    assert resp.status_code == 400
    calls["mock"].chat_completions.assert_not_awaited()


# --- Sensitivity classifier (Day 4b) ----------------------------------------


@pytest.mark.asyncio
async def test_classifier_flags_secret_in_prompt(chat_client):
    """Day 4b: a request with a secret in the user prompt is captured as 'secret'.

    Day 4c: prompt_body is suppressed (None) and response_body is suppressed too.
    """
    c, calls = chat_client

    calls["mock"].chat_completions.return_value = {
        "choices": [{"message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1},
    }

    resp = await c.post(
        "/v1/chat/completions",
        headers={"Authorization": "Bearer ng_test"},
        json={
            "model": "x",
            "messages": [
                {
                    "role": "user",
                    "content": "rotate this for me: ghp_aBcDeFgHiJkLmNoPqRsTuVwXyZ0123456789ab",
                }
            ],
        },
    )
    assert resp.status_code == 200
    pc = calls["precapture"][0]
    assert pc["classified_sensitivity"] == "secret"
    assert any(s["rule_id"] == "github_pat" for s in pc["classified_signals"])
    # Day 4c: secret → no body capture.
    assert pc["prompt_body"] is None
    oc = calls["outcome"][0]
    assert oc["response_body"] is None


@pytest.mark.asyncio
async def test_classifier_flags_pii_when_no_secret(chat_client):
    """Day 4b: PII detected. Day 4c: prompt_body captured but PII spans redacted."""
    c, calls = chat_client

    calls["mock"].chat_completions.return_value = {
        "choices": [{"message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1},
    }

    resp = await c.post(
        "/v1/chat/completions",
        headers={"Authorization": "Bearer ng_test"},
        json={
            "model": "x",
            "messages": [{"role": "user", "content": "email me at hello@48nauts.com"}],
        },
    )
    assert resp.status_code == 200
    pc = calls["precapture"][0]
    assert pc["classified_sensitivity"] == "pii"
    # Day 4c: pii → body kept but matched spans replaced.
    assert pc["prompt_body"] is not None
    assert "[email-redacted]" in pc["prompt_body"]
    assert "@48nauts.com" not in pc["prompt_body"]


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


@pytest.mark.asyncio
async def test_tools_request_skips_the_subscription_lane(chat_app):
    """A tool call must never be claimed by the Codex CLI lane.

    That lane has no function calling, so claiming it answers
    `502 tool calls are not supported by this transport` and the caller sees an
    agent that talks instead of acting. Between 2026-08-18 22:30 and 08-19 this
    disabled every xNAUT agent: no profile changed, the route underneath did.
    """
    app, calls = chat_app
    app.state.settings.nautgate_chatgpt_subscription_cli = True
    app.state.chatgpt_subscription = AsyncMock()
    calls["mock"].chat_completions.return_value = {
        "choices": [{"message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1},
    }

    tools = [
        {
            "type": "function",
            "function": {"name": "ping", "parameters": {"type": "object", "properties": {}}},
        }
    ]
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://nautgate.test") as c:
        resp = await c.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer ng_test"},
            json={
                "model": "gpt-4o-mini",
                "messages": [{"role": "user", "content": "hi"}],
                "tools": tools,
            },
        )

    assert resp.status_code == 200
    assert resp.headers["x-nautgate-provider"] != "chatgpt-subscription"
    # It went to NautRouter's metered key, tools intact.
    app.state.chatgpt_subscription.chat_completions.assert_not_awaited()
    forwarded = calls["mock"].chat_completions.call_args.args[0]
    assert forwarded["tools"] == tools


@pytest.mark.asyncio
async def test_same_request_without_tools_still_uses_the_subscription(chat_app):
    """The guard must be about tools, not about the model. Widening it to skip
    the lane entirely would silently start billing the metered key for every
    request the ChatGPT plan already covers."""
    app, calls = chat_app
    app.state.settings.nautgate_chatgpt_subscription_cli = True
    app.state.chatgpt_subscription = AsyncMock()
    app.state.chatgpt_subscription.chat_completions.return_value = {
        "choices": [{"message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1},
    }

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://nautgate.test") as c:
        resp = await c.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer ng_test"},
            json={"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "hi"}]},
        )

    assert resp.status_code == 200
    assert resp.headers["x-nautgate-provider"] == "chatgpt-subscription"
