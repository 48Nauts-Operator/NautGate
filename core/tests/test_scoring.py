"""Day 5a — 14-dimension scorer + tier mapping."""

import pytest

from app.scoring import (
    DIMENSIONS,
    ResolvedRoute,
    ScoreVector,
    load_routing_table,
    resolve,
    score,
    score_and_route,
    to_provider_model,
    to_tier,
)


def _user_msg(text: str) -> dict:
    return {"messages": [{"role": "user", "content": text}]}


# --- Coverage / shape ------------------------------------------------------


def test_score_emits_all_14_dimensions():
    v = score(_user_msg("hi"))
    assert set(v.dimensions.keys()) == set(DIMENSIONS)
    assert len(v.dimensions) == 14
    for name, val in v.dimensions.items():
        assert 0.0 <= val <= 1.0, f"{name}={val} out of [0,1]"


def test_aggregate_is_zero_for_empty_payload():
    v = score({"messages": []})
    assert v.aggregate == pytest.approx(0.0)


# --- Per-dimension signal --------------------------------------------------


def test_token_count_grows_with_length():
    short = score(_user_msg("hi"))
    long = score(_user_msg("x" * 8000))
    assert long.dimensions["token_count"] > short.dimensions["token_count"]


def test_code_blocks_dimension():
    v = score(_user_msg("```python\nprint('x')\n```\n```\nfoo\n```"))
    assert v.dimensions["code_blocks"] > 0


def test_reasoning_markers_dimension():
    v = score(_user_msg("Please explain why this works step by step."))
    assert v.dimensions["reasoning_markers"] > 0


def test_constraint_count_dimension():
    v = score(_user_msg("It must do X and should never do Y. Always ensure Z."))
    assert v.dimensions["constraint_count"] > 0


def test_tool_calls_dimension():
    v = score({"messages": [{"role": "user", "content": "x"}], "tools": [{"name": "calc"}]})
    assert v.dimensions["tool_calls"] == 1.0


def test_image_presence_dimension():
    payload = {
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "what is this?"},
                    {"type": "image", "source": {"data": "..."}},
                ],
            }
        ]
    }
    v = score(payload)
    assert v.dimensions["image_presence"] == 1.0


def test_system_complexity_dimension():
    v = score(
        {
            "messages": [
                {"role": "system", "content": "x" * 4000},
                {"role": "user", "content": "hi"},
            ]
        }
    )
    assert v.dimensions["system_complexity"] >= 0.99


def test_output_format_strict_via_text():
    v = score(_user_msg("Respond with JSON."))
    assert v.dimensions["output_format_strict"] == 1.0


def test_output_format_strict_via_response_format_field():
    v = score(
        {"messages": [{"role": "user", "content": "x"}], "response_format": {"type": "json_object"}}
    )
    assert v.dimensions["output_format_strict"] == 1.0


def test_domain_legal_dimension():
    v = score(_user_msg("draft a contract clause about plaintiff liability and indemnification"))
    assert v.dimensions["domain_legal"] > 0


def test_language_non_english_dimension():
    v = score(_user_msg("これは日本語のテストです。"))
    assert v.dimensions["language_non_english"] > 0.5


def test_multi_turn_dimension():
    payload = {
        "messages": [
            {"role": "user", "content": "x"},
            {"role": "assistant", "content": "y"},
            {"role": "user", "content": "z"},
            {"role": "assistant", "content": "w"},
            {"role": "user", "content": "v"},
        ]
    }
    v = score(payload)
    assert v.dimensions["multi_turn"] > 0


# --- Tier mapping ----------------------------------------------------------


def test_short_chitchat_is_fast():
    v = score(_user_msg("hi"))
    assert to_tier(v) == "fast"


def test_complex_request_is_deep_or_expert():
    """Code + reasoning + constraints + tools should land at deep or expert."""
    payload = {
        "messages": [
            {
                "role": "system",
                "content": "You are a senior staff engineer. " * 100,
            },
            {
                "role": "user",
                "content": (
                    "Explain why this algorithm is O(n log n) step by step and prove the bound. "
                    "It must handle the empty case and should never panic. "
                    "```python\ndef sort(xs): pass\n```\n"
                    "Respond with JSON: {analysis, proof}."
                ),
            },
        ],
        "tools": [{"type": "function", "function": {"name": "run"}}],
    }
    v = score(payload)
    tier = to_tier(v)
    assert tier in ("deep", "expert"), f"got {tier} for v.aggregate={v.aggregate}"


def test_to_tier_uses_thresholds():
    """Threshold-only test — every floor-trigger dimension explicitly zeroed."""
    weights = dict.fromkeys(DIMENSIONS, 1.0 / 14)
    floor_zero = ("code_blocks", "code_inline", "tool_calls", "domain_legal", "domain_medical")

    def vec_target_aggregate(target: float) -> ScoreVector:
        # 5 of 14 dims are floor-triggers and zeroed; spread `target` across the
        # remaining 9 so v.aggregate == target.
        per = target * 14 / 9
        dims = dict.fromkeys(DIMENSIONS, per)
        for k in floor_zero:
            dims[k] = 0.0
        return ScoreVector(dimensions=dims, weights=weights)

    assert to_tier(vec_target_aggregate(0.0)) == "fast"
    assert to_tier(vec_target_aggregate(0.05)) == "fast"
    assert to_tier(vec_target_aggregate(0.20)) == "balanced"  # [0.15, 0.30)
    assert to_tier(vec_target_aggregate(0.40)) == "deep"  # [0.30, 0.50)
    assert to_tier(vec_target_aggregate(0.70)) == "expert"  # [0.50, ∞)


def test_to_tier_floors_on_code():
    """Even a tiny code presence should not route to chitchat-tier model."""
    weights = dict.fromkeys(DIMENSIONS, 1.0 / 14)
    dims = dict.fromkeys(DIMENSIONS, 0.0)
    dims["code_blocks"] = 0.20  # one fenced code block
    assert to_tier(ScoreVector(dims, weights)) == "deep"


def test_to_tier_floors_on_tools():
    """Tools present + multi-turn → expert; tools alone → deep."""
    weights = dict.fromkeys(DIMENSIONS, 1.0 / 14)
    dims = dict.fromkeys(DIMENSIONS, 0.0)
    dims["tool_calls"] = 1.0
    assert to_tier(ScoreVector(dims, weights)) == "deep"
    dims["multi_turn"] = 0.50
    assert to_tier(ScoreVector(dims, weights)) == "expert"


def test_to_tier_heavy_code_is_expert():
    weights = dict.fromkeys(DIMENSIONS, 1.0 / 14)
    dims = dict.fromkeys(DIMENSIONS, 0.0)
    dims["code_blocks"] = 0.60  # 3+ code fences
    assert to_tier(ScoreVector(dims, weights)) == "expert"


def test_to_tier_floors_on_programming_prose():
    """Pure-prose iOS / framework questions must escalate to deep min."""
    # No code, no tools, just a natural-language iOS question.
    v = score(_user_msg("How do I add SwiftUI navigation in iOS?"))
    assert to_tier(v) == "deep", (
        f"iOS prose got {to_tier(v)} (aggregate={v.aggregate}, aux={v.aux})"
    )

    # And a Python question with no code blocks.
    v = score(_user_msg("Best way to refactor this Django view for an API endpoint?"))
    assert to_tier(v) == "deep"

    # Casual chat (no programming markers) stays fast.
    v = score(_user_msg("What's the weather like?"))
    assert to_tier(v) == "fast"


def test_score_emits_programming_aux_signal():
    """The programming-prose signal lives on aux, not dimensions."""
    v = score(_user_msg("Help me with my Swift / SwiftUI / iOS app"))
    assert v.aux.get("domain_programming", 0) > 0
    assert "domain_programming" not in v.dimensions  # aux only


def test_to_tier_floor_does_not_demote():
    """Floor must only escalate, never bring an expert-scored prompt down."""
    weights = dict.fromkeys(DIMENSIONS, 1.0 / 14)
    dims = dict.fromkeys(DIMENSIONS, 0.95)
    assert to_tier(ScoreVector(dims, weights)) == "expert"


# --- Routing table loading -------------------------------------------------


def test_load_routing_table_from_repo():
    from pathlib import Path

    cfg = Path(__file__).resolve().parents[2] / "config" / "routing.yaml"
    table = load_routing_table(cfg)
    assert set(table.keys()) >= {"fast", "balanced", "deep", "expert"}
    for body in table.values():
        assert "primary" in body
        assert body["primary"]["provider"]
        assert body["primary"]["model"]


def test_resolve_returns_route_with_fallback():
    table = {
        "fast": {
            "primary": {"provider": "openai", "model": "gpt-4o-mini"},
            "fallback": {"provider": "anthropic", "model": "claude-haiku-4-5"},
        }
    }
    r = resolve("fast", table)
    assert isinstance(r, ResolvedRoute)
    assert r.provider == "openai"
    assert r.model == "gpt-4o-mini"
    assert r.fallback == ("anthropic", "claude-haiku-4-5")


def test_resolve_unknown_tier_falls_back_to_balanced():
    table = {
        "balanced": {
            "primary": {"provider": "anthropic", "model": "claude-haiku-4-5"},
        },
    }
    r = resolve("nonexistent", table)
    assert r.provider == "anthropic"


def test_load_rejects_malformed_table(tmp_path):
    bad = tmp_path / "bad.yaml"
    bad.write_text("tiers:\n  foo:\n    primary: { provider: openai }\n")
    with pytest.raises(ValueError):
        load_routing_table(bad)


# --- score_and_route glue --------------------------------------------------


def test_score_and_route_returns_full_tuple():
    table = {
        "fast": {
            "primary": {"provider": "p1", "model": "m1"},
            "fallback": {"provider": "p2", "model": "m2"},
        },
        "balanced": {"primary": {"provider": "p3", "model": "m3"}},
        "deep": {"primary": {"provider": "p4", "model": "m4"}},
        "expert": {"primary": {"provider": "p5", "model": "m5"}},
    }
    v, tier, route = score_and_route(_user_msg("hi"), table)
    assert tier == "fast"
    assert route is not None
    assert route.provider == "p1"
    # Same call without a table → no resolved route.
    v2, tier2, route2 = score_and_route(_user_msg("hi"), None)
    assert route2 is None


def test_to_provider_model_helper():
    table = {"fast": {"primary": {"provider": "x", "model": "y"}}}
    assert to_provider_model("fast", table) == ("x", "y")
