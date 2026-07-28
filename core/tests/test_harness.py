"""NAUTGATE-24 — opt-in harness normalization of local-model pseudo tool calls."""

import json

from app.formats import anthropic as ant
from app.harness import promote_text_tool_calls

QWEN = '<tool_call>\n{"name": "web_search", "arguments": {"query": "best pho"}}\n</tool_call>'


# ---- normalizer ----------------------------------------------------------


def test_promote_hermes_tool_call():
    msg, promoted = promote_text_tool_calls({"role": "assistant", "content": QWEN})
    assert promoted
    tc = msg["tool_calls"][0]
    assert tc["function"]["name"] == "web_search"
    assert json.loads(tc["function"]["arguments"]) == {"query": "best pho"}
    assert msg["content"] is None  # the <tool_call> span was stripped


def test_promote_keeps_surrounding_text():
    msg, promoted = promote_text_tool_calls({"content": f"Let me look that up.\n{QWEN}\n"})
    assert promoted
    assert msg["content"] == "Let me look that up."
    assert msg["tool_calls"][0]["function"]["name"] == "web_search"


def test_promote_from_reasoning_field():
    msg, promoted = promote_text_tool_calls({"content": "", "reasoning_content": QWEN})
    assert promoted
    assert msg["tool_calls"][0]["function"]["name"] == "web_search"


def test_promote_parameters_alias_and_bare_json():
    msg, promoted = promote_text_tool_calls(
        {"content": '{"name": "get_time", "parameters": {"tz": "CET"}}'}
    )
    assert promoted
    assert json.loads(msg["tool_calls"][0]["function"]["arguments"]) == {"tz": "CET"}


def test_noop_when_structured_tool_calls_present():
    original = {"content": QWEN, "tool_calls": [{"id": "x", "function": {"name": "f"}}]}
    msg, promoted = promote_text_tool_calls(original)
    assert not promoted and msg is original


def test_noop_on_plain_text():
    msg, promoted = promote_text_tool_calls({"content": "just a normal answer"})
    assert not promoted


# ---- non-stream bridge ---------------------------------------------------


def _resp(content):
    return {
        "choices": [{"message": {"role": "assistant", "content": content}, "finish_reason": "stop"}]
    }


def test_response_to_anthropic_normalize_promotes():
    out = ant.response_to_anthropic(_resp(QWEN), "m", normalize=True)
    tool_uses = [b for b in out["content"] if b["type"] == "tool_use"]
    assert len(tool_uses) == 1
    assert tool_uses[0]["name"] == "web_search"
    assert out["stop_reason"] == "tool_use"


def test_response_to_anthropic_off_by_default_leaves_pseudo_call_as_text():
    out = ant.response_to_anthropic(_resp(QWEN), "m")  # normalize defaults False
    assert not any(b["type"] == "tool_use" for b in out["content"])
    assert out["content"][0]["type"] == "text"


# ---- streaming bridge ----------------------------------------------------


def _sse(obj):
    return f"data: {json.dumps(obj)}\n\n".encode()


def _drive(normalize, text_pieces):
    t = ant.AnthropicStreamTranslator(model="m", normalize=normalize)
    out = b""
    out += b"".join(t.feed(_sse({"choices": [{"delta": {"role": "assistant"}}]})))
    for piece in text_pieces:
        out += b"".join(t.feed(_sse({"choices": [{"delta": {"content": piece}}]})))
    out += b"".join(t.feed(_sse({"choices": [{"delta": {}, "finish_reason": "stop"}]})))
    out += b"".join(t.feed(b"data: [DONE]\n\n"))
    out += b"".join(t.finish())
    return out.decode()


def test_stream_normalize_promotes_split_tool_call():
    # the <tool_call> arrives split across deltas
    events = _drive(
        True,
        ["<tool_call>", '{"name": "web_search", ', '"arguments": {"query": "x"}}', "</tool_call>"],
    )
    assert '"type":"tool_use"' in events
    assert '"name":"web_search"' in events
    assert '"stop_reason":"tool_use"' in events


def test_stream_off_by_default_streams_text():
    events = _drive(False, [QWEN])
    assert '"type":"tool_use"' not in events
    assert "text_delta" in events
    assert '"stop_reason":"end_turn"' in events


def test_real_qwen_multi_call_capture():
    # Verbatim content captured from qwen/qwen3.6-35b-a3b via LM Studio (2026-07-24):
    # two Hermes tool calls, newline before each closing tag, leading blank lines.
    real = (
        '\n\n<tool_call>{"name": "get_weather", "arguments": {"location": "Paris"}}\n</tool_call>\n'
        '<tool_call>{"name": "search_web", "arguments": {"query": "best croissant paris"}}\n</tool_call>'
    )
    msg, promoted = promote_text_tool_calls({"role": "assistant", "content": real})
    assert promoted
    names = [tc["function"]["name"] for tc in msg["tool_calls"]]
    assert names == ["get_weather", "search_web"]
    assert json.loads(msg["tool_calls"][1]["function"]["arguments"]) == {
        "query": "best croissant paris"
    }
    assert msg["content"] is None
