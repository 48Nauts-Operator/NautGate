"""Shadow testing: pure math + message shaping (no DB, no HTTP)."""

from __future__ import annotations

import json

from app.shadow import flatten_messages, shadow_verdict


def test_flatten_plain_and_blocks():
    body = json.dumps(
        [
            {"role": "system", "content": "be brief"},
            {
                "role": "user",
                "content": [{"type": "text", "text": "a"}, {"type": "text", "text": "b"}],
            },
            {"role": "assistant", "content": "prev"},
            {"role": "user", "content": "again"},
        ]
    )
    out = flatten_messages(body)
    assert [m["role"] for m in out] == ["system", "user", "assistant", "user"]
    assert out[1]["content"] == "a\nb"


def test_flatten_rejects_non_text_blocks():
    body = json.dumps([{"role": "user", "content": [{"type": "tool_result", "content": "x"}]}])
    assert flatten_messages(body) is None
    assert flatten_messages(None) is None
    assert flatten_messages("not json") is None
    # tool role → not mirrorable
    assert flatten_messages(json.dumps([{"role": "tool", "content": "x"}])) is None


def test_flatten_dict_wrapped():
    body = json.dumps({"messages": [{"role": "user", "content": "hi"}]})
    assert flatten_messages(body) == [{"role": "user", "content": "hi"}]


def test_shadow_verdict_non_inferior():
    # 99 ok out of 100 (99%) against p0=0.90 → z≈2.83, significant.
    # (Note 48/50 = 96% is NOT significant vs a 90% bar — small n needs a big edge.)
    v = shadow_verdict(["challenger"] * 80 + ["tie"] * 19 + ["champion"] * 1)
    assert v["n"] == 100 and v["ok_pct"] == 0.99
    assert v["non_inferior"] is True and v["p_value"] < 0.05


def test_shadow_verdict_inferior():
    # 60% ok rate → nowhere near the 90% bar.
    v = shadow_verdict(["challenger"] * 6 + ["champion"] * 4)
    assert v["non_inferior"] is False


def test_shadow_verdict_small_n_withholds():
    v = shadow_verdict(["challenger"] * 5)
    assert v["n"] == 5 and v["p_value"] is None and v["non_inferior"] is None


def test_shadow_verdict_ignores_errors():
    v = shadow_verdict(["error", "challenger", "error", "tie"])
    assert v["n"] == 2 and v["wins"] == 1 and v["ties"] == 1


# ── Prompt diet ─────────────────────────────────────────────────────────────

from app.diet import apply_diet_to_payload, prune_messages  # noqa: E402


def _msgs(n):
    return [{"role": "user" if i % 2 == 0 else "assistant", "content": f"m{i}"} for i in range(n)]


def test_prune_keeps_system_and_tail():
    msgs = [{"role": "system", "content": "sys"}] + _msgs(10)
    out = prune_messages(msgs, "history-6")
    assert out[0]["role"] == "system"
    assert [m["content"] for m in out[1:]] == ["m4", "m5", "m6", "m7", "m8", "m9"]
    assert msgs[1]["content"] == "m0"  # input not mutated


def test_prune_noop_and_unknown():
    assert prune_messages(_msgs(4), "history-6") is None  # nothing to remove
    assert prune_messages(_msgs(20), "nope") is None  # unknown strategy
    assert prune_messages("bad", "history-6") is None


def test_apply_diet_to_payload():
    payload = {"model": "m", "messages": _msgs(12)}
    note = apply_diet_to_payload(payload, "history-2")
    assert note["dropped_messages"] == 10
    assert len(payload["messages"]) == 2
    assert 0 < note["saved_pct"] < 1
    assert note["original_bytes"] > note["pruned_bytes"]


def test_apply_diet_skips_tool_calls():
    payload = {"model": "m", "messages": _msgs(12), "tools": [{"name": "x"}]}
    assert apply_diet_to_payload(payload, "history-2") is None
    assert len(payload["messages"]) == 12  # untouched


# ── Improvement simulation helpers ──────────────────────────────────────────

from app.shadow import replace_last_user  # noqa: E402


def test_replace_last_user():
    msgs = [
        {"role": "system", "content": "s"},
        {"role": "user", "content": "old q"},
        {"role": "assistant", "content": "a"},
        {"role": "user", "content": "final q"},
    ]
    out = replace_last_user(msgs, "better q")
    assert out[3]["content"] == "better q"
    assert out[1]["content"] == "old q"  # earlier user turn untouched
    assert msgs[3]["content"] == "final q"  # input not mutated
    assert replace_last_user([{"role": "assistant", "content": "a"}], "x") is None
    assert replace_last_user(msgs, "") is None


def test_openrouter_claude_id():
    from app.shadow import openrouter_claude_id

    assert openrouter_claude_id("claude-opus-4-8") == "openrouter/anthropic/claude-opus-4.8"
    assert (
        openrouter_claude_id("claude-haiku-4-5-20251001") == "openrouter/anthropic/claude-haiku-4.5"
    )
    assert openrouter_claude_id("claude-fable-5") == "openrouter/anthropic/claude-fable-5"
    assert openrouter_claude_id("claude-sonnet-4-6") == "openrouter/anthropic/claude-sonnet-4.6"
    assert openrouter_claude_id("claude-opus-4") == "openrouter/anthropic/claude-opus-4"
