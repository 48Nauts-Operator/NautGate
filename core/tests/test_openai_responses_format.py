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
async def responses_app(monkeypatch, empty_db_pool):
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
        application.state.db = empty_db_pool
        # The operator's own NAUTGATE_CHATGPT_SUBSCRIPTION_CLI is loaded into the
        # process at startup, and with the lane on, any explicitly-named gpt
        # model bypasses the mocked nautrouter below and shells out to the real
        # Codex CLI (exit 1 -> 502). Setting the env var is not enough: settings
        # are resolved once and cached, so an earlier test in the suite fixes the
        # value before this fixture runs. Pin the resolved objects instead.
        # These tests cover the responses FORMAT, not provider selection.
        if getattr(application.state, "settings", None) is not None:
            application.state.settings.nautgate_chatgpt_subscription_cli = False
        application.state.chatgpt_subscription = None
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
async def test_responses_streaming_emits_responses_event_set(responses_app):
    import json as _json

    app, calls = responses_app

    def _data(payload: dict) -> bytes:
        return f"data: {_json.dumps(payload)}\n\n".encode()

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
            "/v1/responses",
            headers={"Authorization": "Bearer ng_test"},
            json={"model": "gpt-4o", "input": "hi", "stream": True},
        ) as resp:
            assert resp.status_code == 200
            assert resp.headers["content-type"].startswith("text/event-stream")
            received = b""
            async for chunk in resp.aiter_raw():
                received += chunk

    blob = received.decode()
    # Order check — every Responses event in the right sequence.
    expected_events = [
        "response.created",
        "response.output_item.added",
        "response.content_part.added",
        "response.output_text.delta",
        "response.output_text.delta",
        "response.output_text.done",
        "response.content_part.done",
        "response.output_item.done",
        "response.completed",
    ]
    seen = [line.split(": ", 1)[1] for line in blob.split("\n") if line.startswith("event:")]
    assert seen == expected_events, seen
    assert "Hi" in blob and "there" in blob
    # No raw OpenAI Chat shapes leaking through.
    assert '"choices"' not in blob
    # Final response.completed carries usage.
    assert '"input_tokens":4' in blob
    assert '"output_tokens":2' in blob


@pytest.mark.asyncio
async def test_responses_streaming_handles_empty_stream(responses_app):
    """Empty upstream stream still emits well-formed terminators."""
    app, calls = responses_app

    async def stream(_payload, **_kwargs):
        return
        yield  # pragma: no cover

    calls["mock"].chat_completions_stream = stream

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://nautgate.test") as c:
        async with c.stream(
            "POST",
            "/v1/responses",
            headers={"Authorization": "Bearer ng_test"},
            json={"model": "gpt-4o", "input": "hi", "stream": True},
        ) as resp:
            received = b""
            async for chunk in resp.aiter_raw():
                received += chunk

    blob = received.decode()
    # No content events, but created + completed should still fire.
    assert "response.created" in blob
    assert "response.completed" in blob


# Stand-alone translator tests (no HTTP)


def test_translator_stand_alone_emits_full_sequence():
    import json as _json

    from app.formats.openai_responses import ResponsesStreamTranslator

    def _data(d: dict) -> bytes:
        return f"data: {_json.dumps(d)}\n\n".encode()

    t = ResponsesStreamTranslator(model="gpt-4o")
    out: list[bytes] = []
    out += t.feed(_data({"choices": [{"index": 0, "delta": {"role": "assistant"}}]}))
    out += t.feed(_data({"choices": [{"index": 0, "delta": {"content": "x"}}]}))
    out += t.feed(_data({"choices": [{"index": 0, "delta": {"content": "y"}}]}))
    out += t.feed(
        _data(
            {
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 3, "completion_tokens": 2},
            }
        )
    )
    out += t.feed(b"data: [DONE]\n\n")
    blob = b"".join(out).decode()
    # Only count `event:` lines (the JSON `"type": ...` field also contains the string).
    delta_events = [
        line for line in blob.split("\n") if line == "event: response.output_text.delta"
    ]
    assert len(delta_events) == 2
    assert "response.completed" in blob


def test_translator_split_chunks():
    import json as _json

    from app.formats.openai_responses import ResponsesStreamTranslator

    full = f"data: {_json.dumps({'choices': [{'delta': {'content': 'hi'}}]})}\n\n".encode()
    t = ResponsesStreamTranslator(model="x")
    a = t.feed(full[: len(full) // 2])
    assert a == []
    b = t.feed(full[len(full) // 2 :])
    blob = b"".join(b).decode()
    assert "hi" in blob
