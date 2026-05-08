"""sb-brain core logic — produces routing hints from observed history.

The brain is *advisory*. NautGate's pipeline applies hints via the precedence
ladder (Tech Paper §2.5) but never blocks on the brain — if we time out or
return junk, NautGate falls through to score-based routing.

v1 hint sources:
  - **Provider health (last 6h):** if (provider, model) has empty_rate > 30%
    over a meaningful sample, mark it for demotion.
  - **Agent preferences:** mirror routing_preferences (banned + preferred).
  - **Tier nudge:** if the agent's last 7 days skewed heavily toward a tier
    different from the score-derived one, suggest the agent's modal tier.

Future v2 sources (deferred): per-prompt-pattern memory similarity, learned
weights, latency-based demotion. Not in v1.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any

from queries import (
    empty_rate_by_provider_model,
    get_routing_preferences,
    per_agent_recent_tier_distribution,
)

# Tunables.
EMPTY_RATE_DEMOTE_THRESHOLD = 0.30
EMPTY_RATE_MIN_SAMPLE = 3  # don't demote on 1 bad call
TIER_NUDGE_MIN_RATIO = 0.6  # this agent's modal tier must be ≥60% of recent decisions

CACHE_TTL_S = 300.0
CACHE_MAX_ENTRIES = 1000


@dataclass
class _CacheEntry:
    value: dict
    expires_at: float


class HintCache:
    """LRU + TTL cache per Tech Paper §12.3. Keyed by agent_id.

    On `invalidate(agent_id)` (called when on_outcome lands) we drop that
    agent's entry so the next routing decision sees fresh data.
    """

    def __init__(self, max_entries: int = CACHE_MAX_ENTRIES, ttl_s: float = CACHE_TTL_S):
        self._max = max_entries
        self._ttl = ttl_s
        self._entries: dict[str, _CacheEntry] = {}
        self._access_order: list[str] = []

    def get(self, agent_id: str, now: float | None = None) -> dict | None:
        now = now or time.monotonic()
        entry = self._entries.get(agent_id)
        if entry is None:
            return None
        if entry.expires_at < now:
            self._entries.pop(agent_id, None)
            if agent_id in self._access_order:
                self._access_order.remove(agent_id)
            return None
        # Move to end (most recently used).
        if agent_id in self._access_order:
            self._access_order.remove(agent_id)
        self._access_order.append(agent_id)
        return entry.value

    def put(self, agent_id: str, value: dict, now: float | None = None) -> None:
        now = now or time.monotonic()
        if agent_id in self._access_order:
            self._access_order.remove(agent_id)
        self._entries[agent_id] = _CacheEntry(value=value, expires_at=now + self._ttl)
        self._access_order.append(agent_id)
        while len(self._access_order) > self._max:
            evict = self._access_order.pop(0)
            self._entries.pop(evict, None)

    def invalidate(self, agent_id: str) -> None:
        self._entries.pop(agent_id, None)
        if agent_id in self._access_order:
            self._access_order.remove(agent_id)

    def size(self) -> int:
        return len(self._entries)


@dataclass
class HintBundle:
    """The shape NautGate's `before_route` plugin call expects back."""

    brain_hints: dict = field(default_factory=dict)
    banned_models: list[str] = field(default_factory=list)
    preferred_tier: str | None = None
    promoted_models: list[str] = field(default_factory=list)
    demoted_models: list[str] = field(default_factory=list)
    override_model: str | None = None
    reason: str | None = None

    def to_response(self) -> dict:
        out: dict[str, Any] = {}
        if self.brain_hints:
            out["brain_hints"] = self.brain_hints
        if self.banned_models:
            out["banned_models"] = list(dict.fromkeys(self.banned_models))
        if self.preferred_tier:
            out["preferred_tier"] = self.preferred_tier
        if self.promoted_models:
            out["promoted_models"] = list(dict.fromkeys(self.promoted_models))
        if self.demoted_models:
            out["demoted_models"] = list(dict.fromkeys(self.demoted_models))
        if self.override_model:
            out["override_model"] = self.override_model
        if self.reason:
            out["brain_hints"] = {**out.get("brain_hints", {}), "reason": self.reason}
        return out


async def compute_hints(
    pool,
    *,
    agent_id: str,
    classified_tier: str,
    cache: HintCache,
    timeout_s: float = 0.05,
) -> HintBundle:
    """Compute the hint bundle for one request. Honors the 50ms budget — on
    timeout, returns whatever we have so far (possibly empty).

    Cache is consulted first per Tech Paper §12.3.
    """
    cached = cache.get(agent_id)
    if cached is not None:
        # Cache stores tier_distribution alongside the bundle so we can
        # recompute the tier nudge against the *current* request's tier
        # without another DB roundtrip.
        bundle_kwargs = {k: v for k, v in cached.items() if not k.startswith("_")}
        bundle = HintBundle(**bundle_kwargs)
        bundle.preferred_tier = _tier_nudge(cached.get("_tier_distribution") or {}, classified_tier)
        return bundle

    bundle = HintBundle()
    try:
        async with asyncio.timeout(timeout_s):
            empties, prefs, dist = await asyncio.gather(
                empty_rate_by_provider_model(pool, hours=6),
                get_routing_preferences(pool, agent_id=agent_id),
                per_agent_recent_tier_distribution(pool, agent_id=agent_id, days=7),
            )
    except (TimeoutError, Exception):
        # Best-effort — return whatever we have, no caching.
        return bundle

    # Provider-health demotions.
    for (_provider, model), rate in empties.items():
        if rate >= EMPTY_RATE_DEMOTE_THRESHOLD:
            bundle.demoted_models.append(model)

    # Mirror routing_preferences if the agent has them.
    if prefs:
        bundle.banned_models.extend(prefs["banned_models"])
        bundle.promoted_models.extend(prefs["preferred_models"])

    # Tier nudge (only used when score confidence is low; NautGate decides whether to honor).
    bundle.preferred_tier = _tier_nudge(dist, classified_tier)

    if bundle.demoted_models or bundle.banned_models or bundle.preferred_tier:
        bits = []
        if bundle.demoted_models:
            bits.append(f"empty_rate≥{EMPTY_RATE_DEMOTE_THRESHOLD:.0%}: {bundle.demoted_models}")
        if bundle.preferred_tier:
            bits.append(f"agent_modal_tier={bundle.preferred_tier}")
        bundle.reason = "; ".join(bits) or None

    # Cache the parts that don't depend on `classified_tier` so future requests
    # for this agent skip the DB roundtrip even when the tier is different.
    cache.put(
        agent_id,
        {
            "brain_hints": bundle.brain_hints,
            "banned_models": bundle.banned_models,
            "preferred_tier": None,  # always recomputed against current tier
            "promoted_models": bundle.promoted_models,
            "demoted_models": bundle.demoted_models,
            "override_model": bundle.override_model,
            "reason": bundle.reason,
            "_tier_distribution": dist,
        },
    )
    return bundle


def _tier_nudge(distribution: dict[str, int], current_tier: str) -> str | None:
    """If the agent's modal tier (≥60% of recent picks) differs from the
    current scored tier, suggest the modal one.
    """
    if not distribution:
        return None
    total = sum(distribution.values())
    if total < 5:
        return None
    modal_tier, modal_count = max(distribution.items(), key=lambda kv: kv[1])
    if modal_tier == current_tier:
        return None
    if modal_count / total < TIER_NUDGE_MIN_RATIO:
        return None
    return modal_tier
