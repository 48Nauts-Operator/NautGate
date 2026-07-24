"""nautproxy addon — pure parsing/shaping (no mitmproxy needed)."""

import json

from proxy.codex_capture import _build_turn, _consume, _usage_from_responses


def _c(pending, content_obj_or_bytes, from_client, ts=None, flow_id="f1"):
    content = content_obj_or_bytes if isinstance(content_obj_or_bytes, bytes) else json.dumps(content_obj_or_bytes).encode()
    return _consume(pending, flow_id, content, from_client, ts)


def test_consume_emits_turn_on_completed():
    pending: dict = {}
    # create → buffered, nothing emitted yet (this is the live-capture fix)
    assert _c(pending, {"type": "response.create", "model": "gpt-5-codex", "input": [{"role": "user"}]}, True, ts=1.0) is None
    # a delta frame in between → ignored
    assert _c(pending, {"type": "response.output_text.delta"}, False) is None
    # completed → emits the paired turn immediately
    out = _c(pending, {"type": "response.completed", "response": {"model": "gpt-5-codex-2026", "usage": {"input_tokens": 100, "output_tokens": 20}}}, False, ts=2.5)
    assert out is not None
    req, resp, start, end = out
    assert req["model"] == "gpt-5-codex"
    assert resp["model"] == "gpt-5-codex-2026"
    assert (start, end) == (1.0, 2.5)
    assert pending == {}  # consumed


def test_consume_ignores_garbage_and_unpaired():
    pending: dict = {}
    assert _c(pending, b"not-json", True) is None
    assert _c(pending, {"type": "response.completed"}, False) is None  # no pending create
    assert _c(pending, {"type": "response.create", "model": "m"}, True) is None  # buffered, not emitted
    assert pending  # a create is now pending


def test_consume_two_sequential_turns():
    pending: dict = {}
    _c(pending, {"type": "response.create", "model": "m"}, True, ts=1.0)
    assert _c(pending, {"type": "response.completed", "response": {"model": "m"}}, False, ts=2.0) is not None
    _c(pending, {"type": "response.create", "model": "m"}, True, ts=3.0)
    out2 = _c(pending, {"type": "response.completed", "response": {"model": "m"}}, False, ts=4.0)
    assert out2 is not None and out2[2:] == (3.0, 4.0)


def test_usage_subtracts_cached_from_input():
    resp = {"usage": {"input_tokens": 100, "output_tokens": 20, "input_tokens_details": {"cached_tokens": 30}, "output_tokens_details": {"reasoning_tokens": 5}}}
    u = _usage_from_responses(resp)
    assert u["prompt_tokens"] == 70  # 100 total - 30 cached = fresh
    assert u["cache_read_tokens"] == 30
    assert u["completion_tokens"] == 20
    assert u["reasoning_tokens"] == 5


def test_usage_empty_when_no_usage():
    assert _usage_from_responses({}) == {}


def test_build_turn_shapes_ingest_contract():
    req = {"model": "gpt-5-codex", "input": [{"role": "user", "content": "hi"}], "tools": [{"name": "x"}]}
    resp = {"model": "gpt-5-codex-2026", "usage": {"input_tokens": 10, "output_tokens": 3}}
    turn = _build_turn("codex", "openai_responses_ws", "chatgpt-oauth", "127.0.0.1", req, resp, 10.0, 11.0)
    assert turn["agent_id"] == "codex"
    assert turn["inbound_format"] == "openai_responses_ws"
    assert turn["provider"] == "chatgpt-oauth"
    assert turn["model"] == "gpt-5-codex"
    assert turn["served_model"] == "gpt-5-codex-2026"  # attested from response
    assert turn["messages"] == req["input"]
    assert turn["tools"] == req["tools"]
    assert turn["duration_ms"] == 1000
    assert turn["usage"]["prompt_tokens"] == 10
