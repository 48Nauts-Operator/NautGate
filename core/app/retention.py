"""Body retention — drop captured prompt/response text after a fixed window.

``route_decisions`` reached 13 GB of a 14 GB database on a real instance, growing
roughly 400 MB/day, and every nightly dump copied all of it: ~8 GB per backup,
seven minutes per run. The volume is entirely the captured bodies. The metadata
that makes the audit log an audit log — who called what, which model actually
answered, cost, timings, attestation — is a few hundred megabytes and is never
touched here.

So this nulls the bodies past a cutoff and keeps every row. An old call still
tells you it happened, what it cost and which model served it; you just can't
re-read its text.

Note on disk: nulling shrinks *dumps* immediately, because pg_dump only copies
live data. It does NOT hand space back to the filesystem — Postgres keeps the
freed pages for reuse. Reclaiming the file itself needs a VACUUM FULL, which
takes an exclusive lock and therefore belongs in a maintenance window, not in a
background task.
"""

from __future__ import annotations

import asyncio

import asyncpg
import structlog

log = structlog.get_logger()

# Long enough that a quiet loop still prunes daily, short enough that a config
# change is picked up without a restart.
_TICK_SECONDS = 3600


async def prune_bodies(pool: asyncpg.Pool, *, retention_days: int) -> dict[str, int]:
    """Null captured bodies older than ``retention_days``. Returns rows touched.

    A non-positive window disables pruning entirely rather than deleting
    everything — the failure mode of a misread config should be "kept too much",
    never "destroyed the audit trail".
    """
    if retention_days <= 0:
        return {"decisions": 0, "outcomes": 0}

    cutoff = f"{int(retention_days)} days"

    decisions = await pool.execute(
        """
        UPDATE nautgate.route_decisions
           SET prompt_body = NULL,
               prompt_body_truncated_at_byte = NULL,
               tools_body = NULL,
               tools_body_truncated_at_byte = NULL
         WHERE ts < now() - $1::interval
           AND (prompt_body IS NOT NULL OR tools_body IS NOT NULL)
        """,
        cutoff,
    )
    outcomes = await pool.execute(
        """
        UPDATE nautgate.route_outcomes
           SET response_body = NULL,
               response_body_truncated_at_byte = NULL
         WHERE ts < now() - $1::interval
           AND response_body IS NOT NULL
        """,
        cutoff,
    )

    def _count(tag: str) -> int:
        # asyncpg returns "UPDATE <n>".
        try:
            return int(tag.rsplit(" ", 1)[1])
        except (IndexError, ValueError):
            return 0

    result = {"decisions": _count(decisions), "outcomes": _count(outcomes)}
    if result["decisions"] or result["outcomes"]:
        log.info("body_retention_pruned", retention_days=retention_days, **result)
    return result


async def run_scheduler(pool: asyncpg.Pool, *, retention_days: int) -> None:
    """Prune on a slow loop. Never fatal — a pruning failure must not take the
    gateway down, and the next tick retries anyway."""
    if retention_days <= 0:
        log.info("body_retention_disabled")
        return
    log.info("body_retention_started", retention_days=retention_days)
    while True:
        try:
            await prune_bodies(pool, retention_days=retention_days)
        except asyncio.CancelledError:
            log.info("body_retention_cancelled")
            raise
        except Exception as exc:
            log.error(
                "body_retention_iteration_failed",
                error=str(exc) or repr(exc),
                error_type=type(exc).__name__,
            )
        await asyncio.sleep(_TICK_SECONDS)
