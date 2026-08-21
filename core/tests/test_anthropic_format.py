"""Week 1b — Anthropic Messages ↔ OpenAI Chat translation."""

import json
from unittest.mock import AsyncMock

import httpx
import pytest
from asgi_lifespan import LifespanManager

from app.formats.anthropic import (
    AnthropicStreamTranslator,
    request_to_openai_chat,
    response_to_anthropic,
)

# --- Request translation ---------------------------------------------------


def test_request_pass_through_basics():
    out = request_to_openai_chat(
        {
            "model": "claude-haiku-4-5",
            "max_tokens": 64,
            "messages": [{"role": "user", "content": "hi"}],
            "temperature": 0.5,
        }
    )
    assert out["model"] == "claude-haiku-4-5"
    assert out["max_tokens"] == 64
    assert out["temperature"] == 0.5
    assert out["messages"] == [{"role": "user", "content": "hi"}]


def test_request_promotes_top_level_system_to_message():
    out = request_to_openai_chat(
        {
            "model": "x",
            "system": "You are helpful",
            "messages": [{"role": "user", "content": "hi"}],
        }
    )
    assert out["messages"][0] == {"role": "system", "content": "You are helpful"}
    assert out["messages"][1] == {"role": "user", "content": "hi"}


def test_request_handles_system_as_block_list():
    out = request_to_openai_chat(
        {
            "model": "x",
            "system": [{"type": "text", "text": "part one"}, {"type": "text", "text": "part two"}],
            "messages": [{"role": "user", "content": "hi"}],
        }
    )
    assert out["messages"][0]["role"] == "system"
    assert "part one" in out["messages"][0]["content"]
    assert "part two" in out["messages"][0]["content"]


def test_request_normalizes_text_blocks_to_string():
    """Single-text-block content collapses to a plain string for cleanliness."""
    out = request_to_openai_chat(
        {
            "model": "x",
            "messages": [{"role": "user", "content": [{"type": "text", "text": "hi"}]}],
        }
    )
    assert out["messages"] == [{"role": "user", "content": "hi"}]


def test_request_translates_image_block():
    out = request_to_openai_chat(
        {
            "model": "x",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "what is this?"},
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": "image/png",
                                "data": "AAA",
                            },
                        },
                    ],
                }
            ],
        }
    )
    blocks = out["messages"][0]["content"]
    assert isinstance(blocks, list)
    assert {b["type"] for b in blocks} == {"text", "image_url"}
    img = next(b for b in blocks if b["type"] == "image_url")
    assert img["image_url"]["url"].startswith("data:image/png;base64,")


def test_request_maps_stop_sequences():
    out = request_to_openai_chat(
        {"model": "x", "messages": [], "stop_sequences": ["\n\nHuman:", "END"]}
    )
    assert out["stop"] == ["\n\nHuman:", "END"]


# --- Response translation --------------------------------------------------


def test_response_translation_shape():
    openai_resp = {
        "id": "chatcmpl-abc",
        "model": "claude-haiku-4-5",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": "Hello"},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 2},
    }
    out = response_to_anthropic(openai_resp)
    assert out["type"] == "message"
    assert out["role"] == "assistant"
    assert out["content"] == [{"type": "text", "text": "Hello"}]
    assert out["stop_reason"] == "end_turn"
    assert out["usage"] == {"input_tokens": 10, "output_tokens": 2}
    assert out["id"]  # carries through or fabricated
    assert out["model"] == "claude-haiku-4-5"


def test_response_maps_finish_reason_length():
    out = response_to_anthropic(
        {
            "choices": [
                {"message": {"content": "..."}, "finish_reason": "length"},
            ],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        }
    )
    assert out["stop_reason"] == "max_tokens"


def test_response_handles_empty_content():
    out = response_to_anthropic(
        {"choices": [{"message": {"content": ""}, "finish_reason": "stop"}], "usage": {}}
    )
    assert out["content"] == []


# --- Stream translator -----------------------------------------------------


def _data(payload: dict) -> bytes:
    return f"data: {json.dumps(payload)}\n\n".encode()


def test_stream_translator_emits_anthropic_events_in_order():
    t = AnthropicStreamTranslator(model="claude-haiku-4-5")
    chunks: list[bytes] = []
    chunks += t.feed(_data({"choices": [{"index": 0, "delta": {"role": "assistant"}}]}))
    chunks += t.feed(_data({"choices": [{"index": 0, "delta": {"content": "Hello"}}]}))
    chunks += t.feed(_data({"choices": [{"index": 0, "delta": {"content": " world"}}]}))
    chunks += t.feed(
        _data(
            {
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 5, "completion_tokens": 2},
            }
        )
    )
    chunks += t.feed(b"data: [DONE]\n\n")

    blob = b"".join(chunks).decode()
    # Order: message_start, content_block_start, two content_block_delta, content_block_stop, message_delta, message_stop
    events = [line.split(": ", 1)[1] for line in blob.split("\n") if line.startswith("event:")]
    assert events == [
        "message_start",
        "content_block_start",
        "content_block_delta",
        "content_block_delta",
        "content_block_stop",
        "message_delta",
        "message_stop",
    ]
    assert "Hello" in blob
    assert " world" in blob
    assert '"stop_reason":"end_turn"' in blob
    assert '"output_tokens":2' in blob


def test_stream_translator_handles_split_chunks():
    """A single SSE event may be split across multiple feed() calls."""
    t = AnthropicStreamTranslator(model="x")
    full = _data({"choices": [{"index": 0, "delta": {"content": "hi"}}]})
    half = len(full) // 2
    out = t.feed(full[:half])  # partial — yields nothing
    assert out == []
    out2 = t.feed(full[half:])  # completes the event
    blob = b"".join(out2).decode()
    assert "hi" in blob


def test_stream_translator_finish_emits_terminators_if_needed():
    """If upstream ends without DONE, finish() flushes the terminators anyway."""
    t = AnthropicStreamTranslator(model="x")
    t.feed(_data({"choices": [{"index": 0, "delta": {"content": "hi"}}]}))
    out = t.finish()
    blob = b"".join(out).decode()
    assert "content_block_stop" in blob
    assert "message_delta" in blob
    assert "message_stop" in blob


# --- HTTP-level integration via /v1/messages -----------------------------


@pytest.fixture
async def messages_app(monkeypatch, empty_db_pool):
    precapture_calls: list[dict] = []
    outcome_calls: list[dict] = []

    async def fake_precapture(pool, **kw):
        precapture_calls.append(kw)

    async def fake_write_outcome(pool, **kw):
        outcome_calls.append(kw)

    async def fake_upsert_health(pool, **kw):
        pass

    async def fake_authenticate(pool, request):
        from fastapi import HTTPException

        auth = request.headers.get("authorization", "")
        if not auth.lower().startswith("bearer ng_"):
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
        application.state.db = empty_db_pool
        mock = AsyncMock()
        application.state.nautrouter = mock
        yield application, {"precapture": precapture_calls, "outcome": outcome_calls, "mock": mock}


@pytest.mark.asyncio
async def test_messages_round_trip_returns_anthropic_shape(messages_app):
    app, calls = messages_app
    calls["mock"].chat_completions.return_value = {
        "id": "chatcmpl-x",
        "model": "claude-haiku-4-5",
        "choices": [
            {"message": {"role": "assistant", "content": "Hello, world."}, "finish_reason": "stop"}
        ],
        "usage": {"prompt_tokens": 5, "completion_tokens": 3},
    }

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://nautgate.test") as c:
        resp = await c.post(
            "/v1/messages",
            headers={"Authorization": "Bearer ng_test"},
            json={
                "model": "claude-haiku-4-5",
                "system": "You are helpful",
                "max_tokens": 100,
                "messages": [{"role": "user", "content": "hi"}],
            },
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["type"] == "message"
    assert body["role"] == "assistant"
    assert body["content"] == [{"type": "text", "text": "Hello, world."}]
    assert body["stop_reason"] == "end_turn"
    assert body["usage"] == {"input_tokens": 5, "output_tokens": 3}

    # Audit row records inbound_format=anthropic.
    pc = calls["precapture"][0]
    assert pc["inbound_format"] == "anthropic"

    # Forwarded payload to NautRouter is OpenAI Chat shape with system promoted.
    forwarded = calls["mock"].chat_completions.call_args.args[0]
    assert forwarded["messages"][0] == {"role": "system", "content": "You are helpful"}
    assert forwarded["messages"][1]["role"] == "user"


@pytest.mark.asyncio
async def test_confidential_oauth_request_routes_local_instead_of_passthrough(
    messages_app, monkeypatch
):
    app, calls = messages_app

    async def confidential_settings(_pool):
        return {
            "confidentiality_routing": {
                "enabled": True,
                "local_model": "lmstudio/qwen-local",
                "route_pii": True,
                "route_secret": True,
                "bowden_enabled": True,
            }
        }

    passthrough = AsyncMock()
    monkeypatch.setattr("app.app_config.get_settings", confidential_settings)
    monkeypatch.setattr("app.anthropic_oauth_forwarder.forward_to_anthropic", passthrough)
    calls["mock"].chat_completions.return_value = {
        "id": "chatcmpl-local",
        "model": "qwen-local",
        "choices": [{"message": {"role": "assistant", "content": "kept local"}}],
        "usage": {"prompt_tokens": 5, "completion_tokens": 2},
    }

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://nautgate.test") as c:
        resp = await c.post(
            "/v1/messages",
            headers={"Authorization": "Bearer sk-ant-oat01-test-account-token"},
            json={
                "model": "claude-opus-5",
                "max_tokens": 100,
                "messages": [{"role": "user", "content": "My IBAN is CH93 0076 2011 6238 5295 7"}],
            },
        )

    assert resp.status_code == 200
    assert resp.headers["x-nautgate-provider"] == "lmstudio"
    assert resp.headers["x-nautgate-model"] == "lmstudio/qwen-local"
    assert resp.headers["x-nautgate-confidentiality"] == "pii"
    assert resp.headers["x-nautgate-data-boundary"] == "local"
    passthrough.assert_not_awaited()
    forwarded = calls["mock"].chat_completions.call_args.args[0]
    assert forwarded["model"] == "lmstudio/qwen-local"
    assert calls["precapture"][0]["agent_id"].startswith("claude-oauth-")


@pytest.mark.asyncio
async def test_clean_oauth_request_stays_on_subscription_passthrough(messages_app, monkeypatch):
    app, calls = messages_app

    async def confidential_settings(_pool):
        return {
            "confidentiality_routing": {
                "enabled": True,
                "local_model": "lmstudio/qwen-local",
                "route_pii": True,
                "route_secret": True,
                "bowden_enabled": True,
            }
        }

    async def passthrough(_request):
        from fastapi.responses import JSONResponse

        return JSONResponse({"lane": "oauth"})

    monkeypatch.setattr("app.app_config.get_settings", confidential_settings)
    monkeypatch.setattr("app.anthropic_oauth_forwarder.forward_to_anthropic", passthrough)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://nautgate.test") as c:
        resp = await c.post(
            "/v1/messages",
            headers={"Authorization": "Bearer sk-ant-oat01-test-account-token"},
            json={
                "model": "claude-opus-5",
                "max_tokens": 100,
                "messages": [{"role": "user", "content": "Write a short haiku"}],
            },
        )

    assert resp.status_code == 200
    assert resp.json() == {"lane": "oauth"}
    calls["mock"].chat_completions.assert_not_awaited()


@pytest.mark.asyncio
async def test_messages_streaming_emits_anthropic_sse(messages_app):
    app, calls = messages_app

    # Upstream NautRouter speaks OpenAI Chat SSE — the translator turns this into Anthropic.
    upstream_chunks = [
        _data({"choices": [{"index": 0, "delta": {"role": "assistant"}}]}),
        _data({"choices": [{"index": 0, "delta": {"content": "Hi"}}]}),
        _data({"choices": [{"index": 0, "delta": {"content": " there"}}]}),
        _data(
            {
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 4, "completion_tokens": 2},
            }
        ),
        b"data: [DONE]\n\n",
    ]

    async def stream(_payload, **_kwargs):
        for ch in upstream_chunks:
            yield ch

    calls["mock"].chat_completions_stream = stream

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://nautgate.test") as c:
        async with c.stream(
            "POST",
            "/v1/messages",
            headers={"Authorization": "Bearer ng_test"},
            json={
                "model": "claude-haiku-4-5",
                "max_tokens": 100,
                "messages": [{"role": "user", "content": "hi"}],
                "stream": True,
            },
        ) as resp:
            assert resp.status_code == 200
            assert resp.headers["content-type"].startswith("text/event-stream")

            received = b""
            async for chunk in resp.aiter_raw():
                received += chunk

    blob = received.decode()
    # Confirm we got Anthropic-shaped events, not raw OpenAI SSE.
    assert "event: message_start" in blob
    assert "event: content_block_delta" in blob
    assert "event: message_stop" in blob
    assert "Hi" in blob and "there" in blob
    # And NOT raw OpenAI shapes leaking through.
    assert '"choices"' not in blob


# --- NAUTGATE-2: tool_use / tool_result history preservation ----------------


def test_request_preserves_tool_use_and_tool_result_history():
    payload = {
        "model": "deepseek",
        "messages": [
            {"role": "user", "content": "read config.py"},
            {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "I'll read it."},
                    {
                        "type": "tool_use",
                        "id": "toolu_1",
                        "name": "read_file",
                        "input": {"path": "config.py"},
                    },
                ],
            },
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "toolu_1", "content": "PORT = 8090"},
                ],
            },
            {"role": "user", "content": "now what port?"},
        ],
    }
    out = request_to_openai_chat(payload)["messages"]
    # assistant message carries the tool call
    asst = next(m for m in out if m["role"] == "assistant")
    assert asst["tool_calls"][0]["id"] == "toolu_1"
    assert asst["tool_calls"][0]["function"]["name"] == "read_file"
    assert '"path": "config.py"' in asst["tool_calls"][0]["function"]["arguments"]
    # a {role:tool} message carries the result, linked by id
    tool_msg = next(m for m in out if m["role"] == "tool")
    assert tool_msg["tool_call_id"] == "toolu_1"
    assert tool_msg["content"] == "PORT = 8090"
    # the trailing user text survives too
    assert any(m["role"] == "user" and m["content"] == "now what port?" for m in out)


def test_response_maps_tool_calls_to_tool_use():
    openai_resp = {
        "id": "cmpl_x",
        "model": "deepseek",
        "choices": [
            {
                "finish_reason": "tool_calls",
                "message": {
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_9",
                            "type": "function",
                            "function": {"name": "search", "arguments": '{"q":"port"}'},
                        }
                    ],
                },
            }
        ],
        "usage": {"prompt_tokens": 5, "completion_tokens": 3},
    }
    out = response_to_anthropic(openai_resp)
    tu = next(b for b in out["content"] if b["type"] == "tool_use")
    assert tu["id"] == "call_9" and tu["name"] == "search"
    assert tu["input"] == {"q": "port"}
    assert out["stop_reason"] == "tool_use"
