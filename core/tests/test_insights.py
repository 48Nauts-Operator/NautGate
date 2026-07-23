"""Pure-math tests for the Insights panels (no DB)."""

from __future__ import annotations

from app.insights import (
    efficiency_score,
    ewma_chart,
    simulate_costs,
    substitution_impact,
)


class FakePricing:
    """$1/M input, $2/M output for any (provider, model); None for 'unknown'."""

    def compute_cost(
        self,
        provider,
        model,
        *,
        prompt_tokens,
        completion_tokens,
        cache_read_tokens=None,
        cache_write_tokens=None,
    ):
        if model == "unknown":
            return None
        return (prompt_tokens or 0) * 1e-6 + (completion_tokens or 0) * 2e-6


def test_simulate_costs_reprices_and_counts():
    rows = [
        {"actual_usd": 1.0, "prompt_tokens": 1_000_000, "completion_tokens": 500_000},
        {"actual_usd": 2.0, "prompt_tokens": 2_000_000, "completion_tokens": 0},
    ]
    out = simulate_costs(rows, FakePricing(), ("p", "m"))
    assert out["actual_usd"] == 3.0
    assert out["simulated_usd"] == 4.0  # 1+1 + 2+0
    assert out["savings_usd"] == -1.0
    assert out["priced_calls"] == 2 and out["unpriced_calls"] == 0


def test_simulate_costs_unpriced_target():
    out = simulate_costs(
        [{"actual_usd": 5.0, "prompt_tokens": 1, "completion_tokens": 1}],
        FakePricing(),
        ("p", "unknown"),
    )
    assert out["unpriced_calls"] == 1 and out["simulated_usd"] == 0.0


def test_substitution_impact_detects_drop():
    rows = (
        [{"asked": "opus", "served": "opus", "score": 4.0}] * 10
        + [{"asked": "opus", "served": "flash", "score": 2.0}] * 8
        + [{"asked": "opus", "served": "rare", "score": 1.0}] * 2  # < min_n → dropped
    )
    pairs = substitution_impact(rows)
    assert len(pairs) == 1
    p = pairs[0]
    assert p["asked"] == "opus" and p["served"] == "flash"
    assert p["delta"] == -2.0
    assert p["n_substituted"] == 8 and p["n_as_asked"] == 10
    # identical scores per group → zero variance → p undefined
    assert p["p_value"] is None


def test_substitution_impact_p_value_with_variance():
    rows = [{"asked": "a", "served": "a", "score": s} for s in [4, 5, 4, 5, 4, 5]] + [
        {"asked": "a", "served": "b", "score": s} for s in [1, 2, 1, 2, 1]
    ]
    p = substitution_impact(rows)[0]["p_value"]
    assert p is not None and p < 0.01


def test_ewma_chart_flags_shift():
    values = [10.0] * 20 + [30.0] * 6  # step change
    out = ewma_chart(values)
    assert out["violations"], "step change must violate control limits"
    assert min(out["violations"]) >= 20, "violations start after the shift"
    assert len(out["ewma"]) == len(out["ucl"]) == len(values)


def test_ewma_chart_stable_series_no_violations():
    assert ewma_chart([5.0, 5.1, 4.9, 5.0, 5.05, 4.95] * 4)["violations"] == []
    assert ewma_chart([])["ewma"] == []


def test_efficiency_score_full_components():
    out = efficiency_score(
        {
            "quality": 5.0,
            "irrelevant_share": 0.0,
            "cost_usd": 10.0,
            "waste_usd": 0.0,
            "cache_read_tokens": 100,
            "fresh_prompt_tokens": 0,
            "avg_bloat": 0.0,
        }
    )
    assert out["score"] == 100
    assert set(out["components"]) == {"quality", "relevance", "waste", "cache", "bloat"}


def test_efficiency_score_renormalizes_missing():
    # Only quality available: composite equals that single component.
    out = efficiency_score({"quality": 2.5})
    assert out["score"] == 50 and list(out["components"]) == ["quality"]


def test_efficiency_score_empty():
    assert efficiency_score({})["score"] is None
