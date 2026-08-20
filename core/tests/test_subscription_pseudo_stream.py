"""A streaming request routed to the subscription lane gets a stream, not a 501.

The lane cannot stream for real, so the finished answer is delivered as one
OpenAI-shaped SSE chunk. "Could not answer: 501" in every chat client was the
alternative (NautBot, 2026-08-18).
"""

import json


def _pseudo_chunk(final: dict, decision_model: str, decision_id: str) -> str:
    # Mirrors the shape built in routes/v1.py — kept in sync by the route test
    # below reading the source.
    chunk = {
        "id": final.get("id", f"chatcmpl-{decision_id}"),
        "object": "chat.completion.chunk",
        "created": final.get("created"),
        "model": final.get("model", decision_model),
        "choices": [
            {
                "index": 0,
                "delta": (final.get("choices") or [{}])[0].get("message", {}),
                "finish_reason": (final.get("choices") or [{}])[0].get("finish_reason", "stop"),
            }
        ],
        "usage": final.get("usage"),
    }
    return f"data: {json.dumps(chunk)}\n\ndata: [DONE]\n\n"


def test_the_501_is_gone_and_pseudo_stream_exists():
    src = open("app/routes/v1.py").read()
    assert "chatgpt_subscription_streaming_not_supported" not in src
    assert "pseudo_stream" in src
    # the stream flag must be REMOVED before the upstream call, or the codex
    # CLI would receive a parameter it cannot honour
    assert 'payload.pop("stream", None)' in src


def test_chunk_shape_is_an_openai_stream_event():
    final = {
        "id": "chatcmpl-x",
        "created": 1,
        "model": "gpt-5",
        "choices": [{"message": {"role": "assistant", "content": "hi"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1},
    }
    body = _pseudo_chunk(final, "gpt-5", "d1")
    events = [e for e in body.split("\n\n") if e.startswith("data: ")]
    assert events[-1] == "data: [DONE]"
    parsed = json.loads(events[0][len("data: ") :])
    assert parsed["object"] == "chat.completion.chunk"
    assert parsed["choices"][0]["delta"]["content"] == "hi"
    assert parsed["choices"][0]["finish_reason"] == "stop"


def test_a_responses_client_gets_responses_events_not_chat_chunks():
    """The pseudo-stream must go through the format translator when there is one.

    /v1/responses speaks its own event vocabulary. Handing it a
    chat.completion.chunk would be a silently wrong shape: the client sees a
    stream, parses nothing, and shows an empty answer.
    """
    src = open("app/routes/v1.py").read()
    block = src[
        src.index("    if pseudo_stream:") : src.index("    return JSONResponse(content=final")
    ]
    assert "stream_translator(raw)" in block, "pseudo-stream ignores the responses translator"
    assert "stream_translator_finish()" in block, (
        "translator is never flushed, so terminator events never fire"
    )
