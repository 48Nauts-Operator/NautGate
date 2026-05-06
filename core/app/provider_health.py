"""Day 5c — was_empty → provider_health demotion (Tech Paper §8).

Two layers:

1. ``ProviderHealthTracker`` (in-process). Per (provider, model) it keeps a
   streak of consecutive ``was_empty`` outcomes. After ``UNHEALTHY_THRESHOLD``
   in a row, the pair is marked unhealthy. The first non-empty success resets
   the streak.

2. ``upsert_health()`` updates the DB ``nautgate.provider_health`` per-hour
   rollup so the brain layer can query historical health later.

Routing uses ``ProviderHealthTracker.is_unhealthy()`` via
``app.scoring.resolve_healthy()`` to skip the primary and fall back.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

import asyncpg
import structlog

UNHEALTHY_THRESHOLD = 3

log = structlog.get_logger()


@dataclass
class HealthStats:
    consecutive_empty: int = 0
    is_unhealthy: bool = False


class ProviderHealthTracker:
    """In-process streak tracker. One instance lives on app.state.

    Not durable across restarts; that's fine — three new failures will reproduce
    the unhealthy state immediately. The DB rollup is the durable view.
    """

    def __init__(self, threshold: int = UNHEALTHY_THRESHOLD):
        self.threshold = threshold
        self._stats: dict[tuple[str, str], HealthStats] = defaultdict(HealthStats)

    def record(self, provider: str, model: str, *, was_empty: bool) -> HealthStats:
        s = self._stats[(provider, model)]
        if was_empty:
            s.consecutive_empty += 1
            if s.consecutive_empty >= self.threshold and not s.is_unhealthy:
                s.is_unhealthy = True
                log.warning(
                    "provider_marked_unhealthy",
                    provider=provider,
                    model=model,
                    consecutive_empty=s.consecutive_empty,
                )
        else:
            if s.is_unhealthy:
                log.info("provider_recovered", provider=provider, model=model)
            s.consecutive_empty = 0
            s.is_unhealthy = False
        return s

    def is_unhealthy(self, provider: str, model: str) -> bool:
        return self._stats[(provider, model)].is_unhealthy

    def snapshot(self) -> dict[tuple[str, str], HealthStats]:
        return dict(self._stats)


async def upsert_health(
    pool: asyncpg.Pool,
    *,
    provider: str,
    model: str,
    duration_ms: int,
    was_empty: bool,
    success: bool,
    cost_usd: float | None = None,
) -> None:
    """UPSERT the per-hour rollup row for (provider, model)."""
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO nautgate.provider_health
                (provider, model, hour_bucket,
                 total_calls, success_calls, empty_calls, total_latency_ms, total_cost_usd)
            VALUES ($1, $2, date_trunc('hour', NOW()), 1, $3::int, $4::int, $5, COALESCE($6, 0))
            ON CONFLICT (provider, model, hour_bucket) DO UPDATE SET
                total_calls = nautgate.provider_health.total_calls + 1,
                success_calls = nautgate.provider_health.success_calls + EXCLUDED.success_calls,
                empty_calls = nautgate.provider_health.empty_calls + EXCLUDED.empty_calls,
                total_latency_ms = nautgate.provider_health.total_latency_ms + $5,
                total_cost_usd = nautgate.provider_health.total_cost_usd + COALESCE($6, 0)
            """,
            provider,
            model,
            1 if success else 0,
            1 if was_empty else 0,
            duration_ms,
            cost_usd,
        )
