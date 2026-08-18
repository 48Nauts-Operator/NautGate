import json

import pytest

from app.chatgpt_subscription import (
    SubscriptionTransportError,
    build_prompt,
    is_chatgpt_subscription_model,
    parse_codex_jsonl,
)


def test_subscription_model_family_is_explicit():
    assert is_chatgpt_subscription_model("gpt-5.6-luna")
    assert is_chatgpt_subscription_model("o3-mini")
    assert not is_chatgpt_subscription_model("auto")
    assert not is_chatgpt_subscription_model("openrouter/openai/gpt-5")


def test_build_prompt_preserves_roles_and_json_contract():
    prompt = build_prompt(
        {
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": "You classify feeds."},
                {"role": "user", "content": "Return the candidates."},
            ],
        }
    )
    assert "[SYSTEM]\nYou classify feeds." in prompt
    assert "[USER]\nReturn the candidates." in prompt
    assert "Return one valid JSON value" in prompt


def test_parse_codex_jsonl_returns_final_message_and_usage():
    events = [
        {"type": "thread.started", "thread_id": "x"},
        {
            "type": "item.completed",
            "item": {"type": "agent_message", "text": '{"ok":true}'},
        },
        {
            "type": "turn.completed",
            "usage": {"input_tokens": 12, "cached_input_tokens": 3, "output_tokens": 5},
        },
    ]
    body = "\n".join(json.dumps(e) for e in events).encode()
    answer, usage = parse_codex_jsonl(body)
    assert answer == '{"ok":true}'
    assert usage["prompt_tokens"] == 12
    assert usage["completion_tokens"] == 5
    assert usage["prompt_tokens_details"]["cached_tokens"] == 3


def test_parse_codex_jsonl_raises_without_answer():
    body = json.dumps(
        {"type": "turn.failed", "error": {"message": "Not logged in"}}
    ).encode()
    with pytest.raises(SubscriptionTransportError, match="Not logged in"):
        parse_codex_jsonl(body)
