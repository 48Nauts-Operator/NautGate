"""Scheduler for LLM-Probing — mirrors app.backup.run_scheduler.

Polls nautgate.llm_probe_config every tick; when a cycle is due, runs the probe
suite against the configured targets. Resolves the judge config + key fresh each
run (so a key rotation is picked up) and never lets a failed cycle tight-loop.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import asyncpg
import structlog

log = structlog.get_logger()


async def run_scheduler(
    pool: asyncpg.Pool, *, pricing, judge_client, tick_seconds: int = 60
) -> None:
    log.info("llm_probe_scheduler_started", tick_seconds=tick_seconds)
    from app.app_config import is_offline, quality_eval_config
    from app.llm_probe import run_probe_cycle

    while True:
        try:
            # Offline / air-gapped: probes fire real calls at providers, so they
            # stand down entirely. See app_config.is_offline.
            if await is_offline(pool):
                await asyncio.sleep(tick_seconds)
                continue
            cfg = await pool.fetchrow(
                "SELECT enabled, interval_hours, targets, next_run_at "
                "FROM nautgate.llm_probe_config WHERE id=1"
            )
            if cfg is None or not cfg["enabled"] or not (cfg["targets"] or []):
                await asyncio.sleep(tick_seconds)
                continue

            now = datetime.now(UTC)
            if cfg["next_run_at"] is None:
                planned = now + timedelta(hours=cfg["interval_hours"])
                await pool.execute(
                    "UPDATE nautgate.llm_probe_config SET next_run_at=$1 WHERE id=1", planned
                )
                await asyncio.sleep(tick_seconds)
                continue

            if now >= cfg["next_run_at"]:
                try:
                    judge_config = await quality_eval_config(pool)
                    await run_probe_cycle(
                        pool=pool,
                        pricing=pricing,
                        judge_client=judge_client,
                        judge_config=judge_config,
                        targets=list(cfg["targets"]),
                    )
                except Exception as exc:
                    log.warning("scheduled_probe_failed", error=str(exc))
                planned = now + timedelta(hours=cfg["interval_hours"])
                await pool.execute(
                    "UPDATE nautgate.llm_probe_config SET last_run_at=$1, next_run_at=$2 WHERE id=1",
                    now,
                    planned,
                )
        except asyncio.CancelledError:
            log.info("llm_probe_scheduler_cancelled")
            raise
        except Exception as exc:
            log.error("llm_probe_scheduler_iteration_failed", error=str(exc))
        await asyncio.sleep(tick_seconds)
