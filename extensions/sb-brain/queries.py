"""SQL queries used by sb-brain. Kept tight per Tech Paper §12.4 — every
query has a 50ms time budget and falls back to no-hint on timeout.
"""

from __future__ import annotations

import asyncpg


async def empty_rate_by_provider_model(
    pool: asyncpg.Pool,
    *,
    hours: int = 6,
) -> dict[tuple[str, str], float]:
    """Compute empty_calls / total_calls per (provider, model) over the last N hours.

    Reads NautGate's own provider_health hourly rollup table (which sb-brain
    has access to via the same agents_postgres / nautgate schema mount).
    Returns {(provider, model): empty_rate}. Models with no calls are skipped.
    """
    rows = await pool.fetch(
        """
        SELECT provider, model,
               SUM(total_calls)::int  AS total,
               SUM(empty_calls)::int  AS empty
          FROM nautgate.provider_health
         WHERE hour_bucket >= date_trunc('hour', NOW() - make_interval(hours => $1))
         GROUP BY provider, model
        """,
        hours,
    )
    out: dict[tuple[str, str], float] = {}
    for r in rows:
        total = r["total"] or 0
        if total == 0:
            continue
        out[(r["provider"], r["model"])] = (r["empty"] or 0) / total
    return out


async def get_routing_preferences(
    pool: asyncpg.Pool,
    *,
    agent_id: str,
) -> dict | None:
    """Read the agent's routing_preferences row, or None if no row exists."""
    row = await pool.fetchrow(
        """
        SELECT preferred_tier_overrides, banned_models, preferred_models, notes
          FROM nautgate.routing_preferences
         WHERE agent_id = $1
        """,
        agent_id,
    )
    if row is None:
        return None
    return {
        "preferred_tier_overrides": row["preferred_tier_overrides"],
        "banned_models": list(row["banned_models"] or []),
        "preferred_models": list(row["preferred_models"] or []),
        "notes": row["notes"],
    }


async def per_agent_recent_tier_distribution(
    pool: asyncpg.Pool,
    *,
    agent_id: str,
    days: int = 7,
) -> dict[str, int]:
    """Count this agent's tier picks over the last N days. Uses route_decisions
    so we see what NautGate actually decided, not what was requested.
    """
    rows = await pool.fetch(
        """
        SELECT classified_tier AS tier, COUNT(*)::int AS n
          FROM nautgate.route_decisions
         WHERE agent_id = $1
           AND ts > NOW() - make_interval(days => $2)
         GROUP BY classified_tier
        """,
        agent_id,
        days,
    )
    return {r["tier"]: r["n"] for r in rows}
