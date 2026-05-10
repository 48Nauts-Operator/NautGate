"""Tests for the brain layer's bloat detection (app/bloat.py)."""

from app.bloat import (
    PENALTY_BY_SEVERITY,
    TIER_ENVELOPE_BYTES,
    aggregate_score_penalty,
    compute_bloat,
)


def _anatomy(system_b=0, tools_b=0, history_b=0, user_b=0):
    """Minimal payload_anatomy fixture."""
    return {
        "system": {"bytes": system_b, "tokens": system_b // 4, "count": 1 if system_b else 0, "items": []},
        "tools":  {"bytes": tools_b,  "tokens": tools_b // 4,  "count": 1 if tools_b else 0,  "items": []},
        "history": {"bytes": history_b, "tokens": history_b // 4, "count": 1 if history_b else 0, "items": []},
        "user":   {"bytes": user_b,   "tokens": user_b // 4,   "count": 1 if user_b else 0,   "items": []},
        "totals": {
            "bytes": system_b + tools_b + history_b + user_b,
            "tokens": (system_b + tools_b + history_b + user_b) // 4,
            "user_pct": user_b / max(1, system_b + tools_b + history_b + user_b),
        },
    }


# --- excessive_context -----------------------------------------------------


def test_excessive_context_fires_when_user_pct_below_1pct():
    # 50 B user out of 50,000 B total → 0.1% — well below 1% threshold.
    a = _anatomy(system_b=2000, history_b=47950, user_b=50)
    findings, waste_usd = compute_bloat(a, classified_tier="balanced", tools_count=0)
    types = [f.finding_type for f in findings]
    assert "excessive_context" in types
    f = next(f for f in findings if f.finding_type == "excessive_context")
    assert f.severity == "crit"  # 0.1% << 0.5% threshold for crit


def test_excessive_context_does_not_fire_for_small_payloads():
    # 50 B user in 1500 B total — same low percentage but absolute size <2KB → skip.
    a = _anatomy(system_b=1450, user_b=50)
    findings, _ = compute_bloat(a, classified_tier="fast", tools_count=0)
    assert not any(f.finding_type == "excessive_context" for f in findings)


def test_excessive_context_does_not_fire_when_user_dominates():
    a = _anatomy(history_b=1000, user_b=20000)  # user_pct ≈ 95%
    findings, _ = compute_bloat(a, classified_tier="balanced", tools_count=0)
    assert not any(f.finding_type == "excessive_context" for f in findings)


# --- history_dominance -----------------------------------------------------


def test_history_dominance_fires_above_80pct():
    # history is 90% of payload, > 10KB → fires.
    a = _anatomy(system_b=500, history_b=18000, user_b=1500)
    findings, _ = compute_bloat(a, classified_tier="balanced", tools_count=0)
    assert any(f.finding_type == "history_dominance" for f in findings)


def test_history_dominance_does_not_fire_below_threshold():
    a = _anatomy(system_b=2000, history_b=5000, user_b=3000)  # history 50%
    findings, _ = compute_bloat(a, classified_tier="balanced", tools_count=0)
    assert not any(f.finding_type == "history_dominance" for f in findings)


# --- unused_capabilities ---------------------------------------------------


def test_unused_capabilities_fires_when_few_tools_invoked():
    # 50 tools shipped, only 2 used → 4% usage, well below 20% threshold.
    a = _anatomy(system_b=1000, tools_b=20000, user_b=200)
    findings, _ = compute_bloat(
        a, classified_tier="balanced",
        tools_count=50, tool_calls_made_count=2,
    )
    f = next((f for f in findings if f.finding_type == "unused_capabilities"), None)
    assert f is not None
    assert "of 50 tools invoked" in f.detail


def test_unused_capabilities_does_not_fire_when_most_tools_used():
    a = _anatomy(tools_b=10000, user_b=200)
    findings, _ = compute_bloat(
        a, classified_tier="balanced",
        tools_count=10, tool_calls_made_count=8,  # 80% usage
    )
    assert not any(f.finding_type == "unused_capabilities" for f in findings)


def test_unused_capabilities_does_not_fire_for_few_tools():
    # 4 tools doesn't trigger — low absolute count, lots of agents declare a few
    # tools "just in case" without it being abusive.
    a = _anatomy(tools_b=5000, user_b=100)
    findings, _ = compute_bloat(
        a, classified_tier="balanced",
        tools_count=4, tool_calls_made_count=0,
    )
    assert not any(f.finding_type == "unused_capabilities" for f in findings)


# --- oversized_for_tier ----------------------------------------------------


def test_oversized_for_tier_fires_above_2x_envelope():
    # fast envelope is 5,000 B. 15,000 B → 3× → fires.
    a = _anatomy(history_b=14000, user_b=1000)
    findings, _ = compute_bloat(a, classified_tier="fast", tools_count=0)
    f = next((f for f in findings if f.finding_type == "oversized_for_tier"), None)
    assert f is not None
    assert "tier 'fast'" in f.detail


def test_oversized_for_tier_does_not_fire_within_envelope():
    a = _anatomy(history_b=4000, user_b=500)  # 4.5KB << 5KB envelope
    findings, _ = compute_bloat(a, classified_tier="fast", tools_count=0)
    assert not any(f.finding_type == "oversized_for_tier" for f in findings)


# --- waste USD estimation --------------------------------------------------


def test_waste_usd_computed_from_input_price():
    a = _anatomy(history_b=18000, user_b=500)
    _, waste = compute_bloat(
        a, classified_tier="balanced",
        tools_count=0, input_price_per_million=10.0,
    )
    # Some waste should be attributed; positive non-zero number.
    assert waste > 0


def test_waste_usd_zero_when_no_pricing():
    a = _anatomy(history_b=18000, user_b=500)
    _, waste = compute_bloat(
        a, classified_tier="balanced",
        tools_count=0, input_price_per_million=None,
    )
    assert waste == 0.0


# --- Empty / pathological inputs ------------------------------------------


def test_no_findings_for_empty_payload():
    findings, waste = compute_bloat(None, classified_tier="fast", tools_count=0)
    assert findings == []
    assert waste == 0.0


def test_no_findings_for_zero_byte_payload():
    a = _anatomy()
    findings, _ = compute_bloat(a, classified_tier="fast", tools_count=0)
    assert findings == []


# --- Aggregate cap ---------------------------------------------------------


def test_aggregate_score_penalty_capped_at_0_10():
    # Build many crit findings — sum would exceed 0.10 without cap.
    from app.bloat import BloatFinding
    crits = [
        BloatFinding("excessive_context", "crit", PENALTY_BY_SEVERITY["crit"], "x", 0),
        BloatFinding("history_dominance", "crit", PENALTY_BY_SEVERITY["crit"], "x", 0),
        BloatFinding("unused_capabilities", "crit", PENALTY_BY_SEVERITY["crit"], "x", 0),
        BloatFinding("oversized_for_tier", "crit", PENALTY_BY_SEVERITY["crit"], "x", 0),
    ]
    # 4 × 0.06 = 0.24 raw, capped to 0.10
    assert aggregate_score_penalty(crits) == 0.10


def test_aggregate_score_penalty_zero_when_no_findings():
    assert aggregate_score_penalty([]) == 0.0


# --- Tier envelope smoke check --------------------------------------------


def test_envelopes_are_monotonic_by_tier():
    """deeper tiers permit larger payloads."""
    e = TIER_ENVELOPE_BYTES
    assert e["fast"] < e["balanced"] < e["deep"] < e["expert"]
