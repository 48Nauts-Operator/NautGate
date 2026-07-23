"""Day 3 — streaming SSE + tee accumulator + 8 MB cap.

Two layers:
  1. Pure StreamCapture / parse_sse_for_outcome unit tests (no HTTP).
  2. HTTP-level streaming round-trip via the chat_app fixture (mocked NautRouter).
"""

import json
from unittest.mock import AsyncMock

import httpx
import pytest
from asgi_lifespan import LifespanManager

from app.streaming import (
    ACCUMULATOR_CAP_BYTES_DEFAULT,
    StreamCapture,
    parse_sse_for_outcome,
)

# =============================================================================
# Pure logic tests — StreamCapture
# =============================================================================


def test_stream_capture_under_cap_keeps_everything():
    cap = StreamCapture(cap_bytes=1024)
    cap.append(b"hello")
    cap.append(b"world")
    assert bytes(cap.accumulator) == b"helloworld"
    assert cap.bytes_seen == 10
    assert cap.was_truncated is False
    assert cap.truncated_at_byte is None


def test_stream_capture_truncates_at_sse_boundary():
    """Beyond the cap, accumulator stops at the last \\n\\n boundary."""
    cap = StreamCapture(cap_bytes=50)
    # First two events fit under 50 bytes; the third overflows.
    e1 = b'data: {"x":1}\n\n'  # 16 bytes
    e2 = b'data: {"y":2}\n\n'  # 16 bytes, cumulative 32
    e3 = b'data: {"z":3-this-makes-us-overflow}\n\n'  # 38 bytes
    cap.append(e1)
    cap.append(e2)
    cap.append(e3)
    assert cap.was_truncated is True
    assert cap.truncated_at_byte is not None
    # Truncation must land on a \n\n boundary so what's captured ends cleanly.
    assert bytes(cap.accumulator).endswith(b"\n\n"), bytes(cap.accumulator)
    # Original two events are present.
    assert bytes(cap.accumulator).startswith(e1 + e2)
    # bytes_seen should track all chunks regardless of truncation.
    assert cap.bytes_seen == len(e1) + len(e2) + len(e3)


def test_stream_capture_handles_huge_burst_in_single_chunk():
    """A 12 MB chunk arriving at once should still truncate at an SSE boundary."""
    cap = StreamCapture(cap_bytes=ACCUMULATOR_CAP_BYTES_DEFAULT)
    # Build a string of identical small SSE events totalling ~12 MB.
    one_event = b'data: {"choices":[{"delta":{"content":"x"}}]}\n\n'
    n = (12 * 1024 * 1024) // len(one_event) + 1
    blob = one_event * n
    assert len(blob) > ACCUMULATOR_CAP_BYTES_DEFAULT

    cap.append(blob)

    assert cap.was_truncated is True
    assert cap.truncated_at_byte is not None
    assert cap.truncated_at_byte <= ACCUMULATOR_CAP_BYTES_DEFAULT
    assert bytes(cap.accumulator).endswith(b"\n\n")
    # Subsequent appends after truncation are no-ops on the accumulator.
    cap.append(b"more")
    assert cap.bytes_seen == len(blob) + len(b"more")
    assert bytes(cap.accumulator).endswith(b"\n\n")


# =============================================================================
# Pure logic tests — parse_sse_for_outcome
# =============================================================================


def _ev(payload: dict) -> bytes:
    return f"data: {json.dumps(payload)}\n\n".encode()


def test_parse_openai_stream_extracts_content_and_usage():
    buf = b"".join(
        [
            _ev({"choices": [{"index": 0, "delta": {"role": "assistant"}}]}),
            _ev({"choices": [{"index": 0, "delta": {"content": "Hello"}}]}),
            _ev({"choices": [{"index": 0, "delta": {"content": " world"}}]}),
            _ev(
                {
                    "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 12, "completion_tokens": 2},
                }
            ),
            b"data: [DONE]\n\n",
        ]
    )
    out = parse_sse_for_outcome(buf)
    assert out["assembled_content"] == "Hello world"
    assert out["finish_reason"] == "stop"
    assert out["prompt_tokens"] == 12
    assert out["completion_tokens"] == 2
    assert out["was_empty"] is False


def test_parse_was_empty_when_tokens_but_no_content():
    """Tongyi failure mode: completion tokens generated but content empty."""
    buf = b"".join(
        [
            _ev(
                {
                    "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 100, "completion_tokens": 50},
                }
            ),
            b"data: [DONE]\n\n",
        ]
    )
    out = parse_sse_for_outcome(buf)
    assert out["assembled_content"] == ""
    assert out["completion_tokens"] == 50
    assert out["was_empty"] is True


def test_parse_anthropic_stream():
    """Verify the Anthropic Messages SSE event shape parses too."""
    buf = b"".join(
        [
            b"event: message_start\n"
            + b'data: {"type":"message_start","message":{"id":"x","role":"assistant","content":[],"usage":{"input_tokens":10}}}\n\n',
            b"event: content_block_start\n"
            + b'data: {"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}\n\n',
            b"event: content_block_delta\n"
            + b'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"Hi"}}\n\n',
            b"event: content_block_delta\n"
            + b'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":" there"}}\n\n',
            b"event: content_block_stop\n" + b'data: {"type":"content_block_stop","index":0}\n\n',
            b"event: message_delta\n"
            + b'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"},"usage":{"output_tokens":2}}\n\n',
            b"event: message_stop\n" + b'data: {"type":"message_stop"}\n\n',
        ]
    )
    out = parse_sse_for_outcome(buf)
    assert out["assembled_content"] == "Hi there"
    assert out["prompt_tokens"] == 10
    assert out["completion_tokens"] == 2
    assert out["finish_reason"] == "end_turn"
    assert out["was_empty"] is False


# =============================================================================
# HTTP-level streaming round-trip
# =============================================================================


@pytest.fixture
async def streaming_app(monkeypatch):
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
        application.state.db = AsyncMock()
        mock = AsyncMock()
        application.state.nautrouter = mock
        yield (
            application,
            {
                "precapture": precapture_calls,
                "outcome": outcome_calls,
                "mock": mock,
            },
        )


def _set_stream_chunks(mock, chunks: list[bytes]):
    """Replace mock.chat_completions_stream with an async generator yielding chunks."""

    async def stream(_payload, **_kwargs):
        for c in chunks:
            yield c

    mock.chat_completions_stream = stream


@pytest.mark.asyncio
async def test_streaming_happy_path(streaming_app):
    app, calls = streaming_app
    chunks = [
        _ev({"choices": [{"index": 0, "delta": {"role": "assistant"}}]}),
        _ev({"choices": [{"index": 0, "delta": {"content": "Hello"}}]}),
        _ev({"choices": [{"index": 0, "delta": {"content": " world"}}]}),
        _ev(
            {
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 8, "completion_tokens": 2},
            }
        ),
        b"data: [DONE]\n\n",
    ]
    _set_stream_chunks(calls["mock"], chunks)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://nautgate.test") as c:
        async with c.stream(
            "POST",
            "/v1/chat/completions",
            headers={"Authorization": "Bearer ng_test"},
            json={
                "model": "claude-haiku-4.5",
                "messages": [{"role": "user", "content": "say hi"}],
                "stream": True,
            },
        ) as resp:
            assert resp.status_code == 200
            assert resp.headers["content-type"].startswith("text/event-stream")
            assert resp.headers.get("x-nautgate-decision-id")

            received = b""
            async for chunk in resp.aiter_raw():
                received += chunk

    assert received == b"".join(chunks), "client must receive every byte upstream sent"

    # Outcome row recorded with parsed metrics.
    assert len(calls["outcome"]) == 1
    oc = calls["outcome"][0]
    assert oc["status_code"] == 200
    assert oc["prompt_tokens"] == 8
    assert oc["completion_tokens"] == 2
    assert oc["was_empty"] is False
    assert oc["was_truncated"] is False
    assert oc["client_disconnected"] is False
    assert oc["first_byte_ms"] is not None  # set after first chunk


@pytest.mark.asyncio
async def test_streaming_was_empty_records_correctly(streaming_app):
    app, calls = streaming_app
    chunks = [
        _ev({"choices": [{"index": 0, "delta": {"role": "assistant"}}]}),
        _ev({"choices": [{"index": 0, "delta": {"content": ""}}]}),
        _ev(
            {
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 50, "completion_tokens": 15},
            }
        ),
        b"data: [DONE]\n\n",
    ]
    _set_stream_chunks(calls["mock"], chunks)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://nautgate.test") as c:
        async with c.stream(
            "POST",
            "/v1/chat/completions",
            headers={"Authorization": "Bearer ng_test"},
            json={
                "model": "x",
                "messages": [{"role": "user", "content": "hi"}],
                "stream": True,
            },
        ) as resp:
            async for _ in resp.aiter_raw():
                pass

    assert calls["outcome"][0]["was_empty"] is True


@pytest.mark.asyncio
async def test_streaming_truncates_capture_but_passes_full_to_client(streaming_app):
    """Client gets every byte even when our accumulator hits the cap."""
    app, calls = streaming_app
    one_event = b'data: {"choices":[{"index":0,"delta":{"content":"x"}}]}\n\n'
    big = one_event * ((ACCUMULATOR_CAP_BYTES_DEFAULT // len(one_event)) + 50)
    final = _ev(
        {
            "choices": [{"index": 0, "delta": {}, "finish_reason": "length"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 9999},
        }
    )
    chunks = [big, final, b"data: [DONE]\n\n"]
    _set_stream_chunks(calls["mock"], chunks)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://nautgate.test") as c:
        async with c.stream(
            "POST",
            "/v1/chat/completions",
            headers={"Authorization": "Bearer ng_test"},
            json={
                "model": "x",
                "messages": [{"role": "user", "content": "hi"}],
                "stream": True,
            },
        ) as resp:
            received = b""
            async for chunk in resp.aiter_raw():
                received += chunk

    expected_total = sum(len(c) for c in chunks)
    assert len(received) == expected_total, (
        "client must see every byte even after capture truncation"
    )

    oc = calls["outcome"][0]
    assert oc["was_truncated"] is True
    assert oc["truncated_at_byte"] is not None
    assert oc["truncated_at_byte"] <= ACCUMULATOR_CAP_BYTES_DEFAULT
