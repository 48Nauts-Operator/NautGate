"""nautproxy addon — pure parsing/shaping (no mitmproxy needed)."""

import json
from dataclasses import dataclass

from proxy.codex_capture import _build_turn, _pair_turns, _usage_from_responses


@dataclass
class FakeMsg:
    from_client: bool
    content: bytes
    timestamp: float | None = None


def _msg(from_client, obj, ts=None):
    return FakeMsg(from_client, json.dumps(obj).encode(), ts)


def test_pair_turns_matches_create_to_completed():
    msgs = [
        _msg(True, {"type": "response.create", "model": "gpt-5-codex", "input": [{"role": "user"}]}, ts=1.0),
        _msg(False, {"type": "response.output_text.delta"}),  # noise between
        _msg(False, {"type": "response.completed", "response": {"model": "gpt-5-codex-2026", "usage": {"input_tokens": 100, "output_tokens": 20}}}, ts=2.5),
    ]
    turns = _pair_turns(msgs)
    assert len(turns) == 1
    req, resp, start, end = turns[0]
    assert req["model"] == "gpt-5-codex"
    assert resp["model"] == "gpt-5-codex-2026"
    assert (start, end) == (1.0, 2.5)


def test_pair_turns_ignores_unpaired_and_garbage():
    msgs = [
        FakeMsg(True, b"not-json"),
        _msg(True, {"type": "response.create", "model": "m"}),  # no completed after
        _msg(False, {"type": "response.completed"}),  # completed with no pending create before it? -> paired with the create above
    ]
    turns = _pair_turns(msgs)
    # the create pairs with the trailing completed; garbage is skipped
    assert len(turns) == 1


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
