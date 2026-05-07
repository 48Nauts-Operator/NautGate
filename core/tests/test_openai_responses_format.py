"""Week 1b — OpenAI Responses ↔ OpenAI Chat translation."""

from unittest.mock import AsyncMock

import httpx
import pytest
from asgi_lifespan import LifespanManager

from app.formats.openai_responses import (
    request_to_openai_chat,
    response_to_openai_responses,
)

# --- Request translation ---------------------------------------------------


def test_request_input_string_becomes_user_message():
    out = request_to_openai_chat({"model": "gpt-4o", "input": "say hi"})
    assert out["model"] == "gpt-4o"
    assert out["messages"] == [{"role": "user", "content": "say hi"}]


def test_request_instructions_becomes_system_message():
    out = request_to_openai_chat({"model": "x", "instructions": "be concise", "input": "hi"})
    assert out["messages"][0] == {"role": "system", "content": "be concise"}
    assert out["messages"][1] == {"role": "user", "content": "hi"}


def test_request_input_list_with_typed_blocks():
    out = request_to_openai_chat(
        {
            "model": "x",
            "input": [
                {
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": "first"},
                    ],
                },
                {
                    "role": "assistant",
                    "content": [
                        {"type": "output_text", "text": "ack"},  # Responses output_text echoed back
                    ],
                },
            ],
        }
    )
    # output_text is unknown to our normalizer → drop it; first user msg passes through.
    assert out["messages"][0]["role"] == "user"
    assert out["messages"][0]["content"] == "first"


def test_request_input_developer_role_becomes_system():
    out = request_to_openai_chat(
        {"model": "x", "input": [{"role": "developer", "content": "follow these rules"}]}
    )
    assert out["messages"][0] == {"role": "system", "content": "follow these rules"}


def test_request_max_output_tokens_renamed():
    out = request_to_openai_chat({"model": "x", "input": "hi", "max_output_tokens": 250})
    assert out["max_tokens"] == 250


def test_request_input_image_block_translated():
    out = request_to_openai_chat(
        {
            "model": "x",
            "input": [
                {
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": "describe"},
                        {"type": "input_image", "image_url": "https://example.com/x.png"},
                    ],
                }
            ],
        }
    )
    blocks = out["messages"][0]["content"]
    assert isinstance(blocks, list)
    types = {b["type"] for b in blocks}
    assert "image_url" in types
    img = next(b for b in blocks if b["type"] == "image_url")
    assert img["image_url"] == {"url": "https://example.com/x.png"}


# --- Response translation --------------------------------------------------


def test_response_translation_shape():
    openai_chat_resp = {
        "id": "chatcmpl-z",
        "model": "gpt-4o",
        "choices": [
            {"message": {"role": "assistant", "content": "Hello!"}, "finish_reason": "stop"}
        ],
        "usage": {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7},
    }
    out = response_to_openai_responses(openai_chat_resp)
    assert out["object"] == "response"
    assert out["status"] == "completed"
    assert out["model"] == "gpt-4o"
    assert out["output_text"] == "Hello!"
    assert out["output"][0]["type"] == "message"
    assert out["output"][0]["content"] == [{"type": "output_text", "text": "Hello!"}]
    assert out["usage"]["input_tokens"] == 5
    assert out["usage"]["output_tokens"] == 2
    assert out["usage"]["total_tokens"] == 7


def test_response_status_for_length_finish():
    out = response_to_openai_responses(
        {
            "choices": [{"message": {"content": "..."}, "finish_reason": "length"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        }
    )
    assert out["status"] == "incomplete"


def test_response_handles_empty_content():
    out = response_to_openai_responses(
        {"choices": [{"message": {"content": ""}, "finish_reason": "stop"}], "usage": {}}
    )
    assert out["output"][0]["content"] == []
    assert out["output_text"] == ""


# --- HTTP-level integration via /v1/responses ------------------------------


@pytest.fixture
async def responses_app(monkeypatch):
    pre, out = [], []

    async def fake_precapture(pool, **kw):
        pre.append(kw)

    async def fake_write_outcome(pool, **kw):
        out.append(kw)

    async def fake_upsert_health(pool, **kw):
        pass

    async def fake_authenticate(pool, request):
        from fastapi import HTTPException

        if not request.headers.get("authorization", "").lower().startswith("bearer ng_"):
            raise HTTPException(status_code=401, detail="bad token")
        return "anonymous"

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
    monkeypatch.setattr("app.routes.v1.upsert_health", fake_upsert_health)
    monkeypatch.setattr("app.routes.v1.authenticate", fake_authenticate)
    monkeypatch.setattr("app.routes.v1.queries.get_routing_preferences", fake_get_prefs)

    from app.main import create_app

    application = create_app()
    async with LifespanManager(application):
        application.state.db = AsyncMock()
        mock = AsyncMock()
        application.state.nautrouter = mock
        yield application, {"precapture": pre, "outcome": out, "mock": mock}


@pytest.mark.asyncio
async def test_responses_round_trip(responses_app):
    app, calls = responses_app
    calls["mock"].chat_completions.return_value = {
        "id": "chatcmpl-x",
        "model": "gpt-4o",
        "choices": [
            {"message": {"role": "assistant", "content": "Hi there!"}, "finish_reason": "stop"}
        ],
        "usage": {"prompt_tokens": 3, "completion_tokens": 3, "total_tokens": 6},
    }

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://nautgate.test") as c:
        resp = await c.post(
            "/v1/responses",
            headers={"Authorization": "Bearer ng_test"},
            json={
                "model": "gpt-4o",
                "instructions": "be brief",
                "input": "say hi",
                "max_output_tokens": 64,
            },
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["object"] == "response"
    assert body["output_text"] == "Hi there!"
    assert body["status"] == "completed"
    assert body["usage"]["input_tokens"] == 3

    pc = calls["precapture"][0]
    assert pc["inbound_format"] == "openai_responses"

    forwarded = calls["mock"].chat_completions.call_args.args[0]
    assert forwarded["messages"][0] == {"role": "system", "content": "be brief"}
    assert forwarded["messages"][1] == {"role": "user", "content": "say hi"}
    assert forwarded["max_tokens"] == 64


@pytest.mark.asyncio
async def test_responses_streaming_returns_501(responses_app):
    """Streaming for /v1/responses is a follow-up — should 501 with the marker."""
    app, _ = responses_app
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://nautgate.test") as c:
        resp = await c.post(
            "/v1/responses",
            headers={"Authorization": "Bearer ng_test"},
            json={"model": "gpt-4o", "input": "hi", "stream": True},
        )
    assert resp.status_code == 501
    assert resp.headers["x-nautgate-coming-in"] == "week-1b-stream"
