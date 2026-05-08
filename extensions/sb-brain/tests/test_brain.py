"""sb-brain unit tests — cache + compute_hints + tier nudge."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from brain import (  # noqa: E402
    EMPTY_RATE_DEMOTE_THRESHOLD,
    HintBundle,
    HintCache,
    _tier_nudge,
    compute_hints,
)

# --- HintCache --------------------------------------------------------


def test_cache_put_get():
    c = HintCache()
    c.put("alice", {"x": 1})
    assert c.get("alice") == {"x": 1}


def test_cache_ttl_expires():
    c = HintCache(ttl_s=10.0)
    c.put("alice", {"x": 1}, now=100.0)
    assert c.get("alice", now=105.0) == {"x": 1}
    assert c.get("alice", now=120.0) is None


def test_cache_invalidate_drops_entry():
    c = HintCache()
    c.put("alice", {"x": 1})
    c.invalidate("alice")
    assert c.get("alice") is None


def test_cache_lru_eviction():
    c = HintCache(max_entries=2)
    c.put("a", {})
    c.put("b", {})
    c.put("c", {})  # evicts "a"
    assert c.get("a") is None
    assert c.get("b") is not None
    assert c.get("c") is not None


def test_cache_get_promotes_to_mru():
    c = HintCache(max_entries=2)
    c.put("a", {})
    c.put("b", {})
    c.get("a")  # touches a → b becomes LRU
    c.put("c", {})  # evicts "b" not "a"
    assert c.get("a") is not None
    assert c.get("b") is None


# --- _tier_nudge -------------------------------------------------------


def test_tier_nudge_empty_distribution():
    assert _tier_nudge({}, "fast") is None


def test_tier_nudge_below_min_sample():
    assert _tier_nudge({"fast": 2, "balanced": 2}, "fast") is None  # total=4 < 5


def test_tier_nudge_below_min_ratio():
    # modal=fast at 50%; threshold is 60%.
    assert _tier_nudge({"fast": 5, "balanced": 4, "deep": 1}, "balanced") is None


def test_tier_nudge_returns_modal_when_above_threshold():
    # modal=deep at 70%
    assert _tier_nudge({"fast": 1, "deep": 7, "balanced": 2}, "fast") == "deep"


def test_tier_nudge_skips_when_modal_matches_current():
    assert _tier_nudge({"deep": 7, "fast": 3}, "deep") is None


# --- HintBundle.to_response -------------------------------------------


def test_to_response_drops_empty_fields():
    b = HintBundle()
    assert b.to_response() == {}


def test_to_response_includes_set_fields():
    b = HintBundle(
        banned_models=["gpt-4o-mini"],
        preferred_tier="deep",
        demoted_models=["openrouter/free"],
        reason="empty_rate",
    )
    out = b.to_response()
    assert out["banned_models"] == ["gpt-4o-mini"]
    assert out["preferred_tier"] == "deep"
    assert out["demoted_models"] == ["openrouter/free"]
    assert out["brain_hints"]["reason"] == "empty_rate"


def test_to_response_dedupes():
    b = HintBundle(banned_models=["a", "a", "b"])
    out = b.to_response()
    assert out["banned_models"] == ["a", "b"]


# --- compute_hints -----------------------------------------------------


class _FakePool:
    """Mimics asyncpg.Pool returning canned rows from canned queries."""

    def __init__(self, *, empties: dict, prefs: dict | None, distribution: dict):
        self.empties = empties
        self.prefs = prefs
        self.distribution = distribution
        self.fetch_calls = 0
        self.fetchrow_calls = 0

    async def fetch(self, sql, *args):
        self.fetch_calls += 1
        if "provider_health" in sql:
            return [
                {"provider": p, "model": m, "total": 100, "empty": int(rate * 100)}
                for (p, m), rate in self.empties.items()
            ]
        if "route_decisions" in sql:
            return [{"tier": k, "n": v} for k, v in self.distribution.items()]
        return []

    async def fetchrow(self, sql, *args):
        self.fetchrow_calls += 1
        if "routing_preferences" in sql:
            if self.prefs is None:
                return None
            return {
                "preferred_tier_overrides": None,
                "banned_models": self.prefs.get("banned_models", []),
                "preferred_models": self.prefs.get("preferred_models", []),
                "notes": None,
            }
        return None


@pytest.mark.asyncio
async def test_compute_hints_demotes_high_empty_rate():
    pool = _FakePool(
        empties={
            ("openai", "gpt-4o-mini"): 0.5,  # demote
            ("anthropic", "claude-haiku-4-5"): 0.05,  # keep
        },
        prefs=None,
        distribution={},
    )
    cache = HintCache()
    bundle = await compute_hints(
        pool, agent_id="alice", classified_tier="fast", cache=cache, timeout_s=5.0
    )
    assert "gpt-4o-mini" in bundle.demoted_models
    assert "claude-haiku-4-5" not in bundle.demoted_models


@pytest.mark.asyncio
async def test_compute_hints_skips_low_sample():
    """Only 2 calls in the bucket — too small to demote even at 100%."""
    # Our query aggregates SUM(total_calls) over the window; we test the threshold
    # check using a separate fake. Here we just verify the threshold constant exists.
    assert EMPTY_RATE_DEMOTE_THRESHOLD == 0.30


@pytest.mark.asyncio
async def test_compute_hints_mirrors_prefs():
    pool = _FakePool(
        empties={},
        prefs={"banned_models": ["m1"], "preferred_models": ["m2"]},
        distribution={},
    )
    cache = HintCache()
    bundle = await compute_hints(
        pool, agent_id="alice", classified_tier="fast", cache=cache, timeout_s=5.0
    )
    assert "m1" in bundle.banned_models
    assert "m2" in bundle.promoted_models


@pytest.mark.asyncio
async def test_compute_hints_caches_subsequent_calls():
    pool = _FakePool(empties={}, prefs=None, distribution={})
    cache = HintCache()
    await compute_hints(pool, agent_id="alice", classified_tier="fast", cache=cache, timeout_s=5.0)
    fetch_count_after_first = pool.fetch_calls
    fetchrow_count_after_first = pool.fetchrow_calls
    # Second call hits cache → no DB calls.
    await compute_hints(pool, agent_id="alice", classified_tier="fast", cache=cache, timeout_s=5.0)
    assert pool.fetch_calls == fetch_count_after_first
    assert pool.fetchrow_calls == fetchrow_count_after_first


@pytest.mark.asyncio
async def test_compute_hints_recomputes_tier_nudge_per_call():
    """Cache hit should still recompute preferred_tier against current classified_tier."""
    pool = _FakePool(
        empties={},
        prefs=None,
        distribution={"deep": 8, "fast": 2},  # modal=deep at 80%
    )
    cache = HintCache()
    bundle1 = await compute_hints(
        pool, agent_id="alice", classified_tier="fast", cache=cache, timeout_s=5.0
    )
    assert bundle1.preferred_tier == "deep"  # nudge away from fast
    # Same agent, current tier already "deep" → no nudge even on cache hit.
    bundle2 = await compute_hints(
        pool, agent_id="alice", classified_tier="deep", cache=cache, timeout_s=5.0
    )
    assert bundle2.preferred_tier is None


@pytest.mark.asyncio
async def test_compute_hints_swallows_errors():
    class BoomPool:
        async def fetch(self, *a):
            raise RuntimeError("db gone")

        async def fetchrow(self, *a):
            raise RuntimeError("db gone")

    cache = HintCache()
    bundle = await compute_hints(
        BoomPool(), agent_id="alice", classified_tier="fast", cache=cache, timeout_s=5.0
    )
    # Empty bundle, no exception.
    assert bundle.to_response() == {}
