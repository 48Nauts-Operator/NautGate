"""Bulk-backfill quality evals against historical decisions.

The Quality Eval system normally runs forward only — it judges a decision
shortly after it lands in route_outcomes, gated by sample rate + anomaly
triggers. Decisions that predate the feature (or were missed during the
401 auth incident) never get evaluated.

This script picks every eligible historical decision and runs the judge
against it. "Eligible" = has captured prompt body + response body, isn't
secret-classified, isn't already evaluated.

Usage (from core/):
    cd core && uv run python ../scripts/quality_backfill.py            # dry-run
    cd core && uv run python ../scripts/quality_backfill.py --apply    # actually evaluate
    cd core && uv run python ../scripts/quality_backfill.py --apply --limit 100
    cd core && uv run python ../scripts/quality_backfill.py --apply --concurrency 8

Cost cap: defaults to $5 (configurable). Stops if reached. Judge config
comes from nautgate.app_config (same as the live system).
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
from pathlib import Path

_CORE = Path(__file__).resolve().parent.parent / "core"
if str(_CORE) not in sys.path:
    sys.path.insert(0, str(_CORE))

import asyncpg  # noqa: E402


def _load_env() -> None:
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    for p in (_CORE / ".env", _CORE.parent / ".env", _CORE.parent / "deploy" / ".env"):
        if p.is_file():
            load_dotenv(p, override=False)


async def _eligible_ids(pool, limit: int | None) -> list[str]:
    sql = """
        SELECT d.id::text AS id
          FROM nautgate.route_decisions d
          JOIN nautgate.route_outcomes  o ON o.decision_id = d.id
          LEFT JOIN nautgate.quality_evals q ON q.decision_id = d.id
         WHERE q.decision_id IS NULL
           AND d.prompt_body IS NOT NULL
           AND o.response_body IS NOT NULL
           AND length(o.response_body) > 20
           AND COALESCE(d.classified_sensitivity, '') <> 'secret'
         ORDER BY d.ts DESC
    """
    if limit:
        sql += f" LIMIT {int(limit)}"
    async with pool.acquire() as conn:
        rows = await conn.fetch(sql)
    return [r["id"] for r in rows]


async def _eval_one(pool, pricing, judge_client, decision_id: str) -> tuple[str, bool, float, str | None]:
    """Run the eval pipeline for one decision_id. Returns (id, success, cost, error)."""
    from app.quality_eval import _call_judge, _get_config, _load_pair, _persist
    cfg = await _get_config(pool)
    if not cfg.get("enabled"):
        return (decision_id, False, 0.0, "disabled")
    decision, outcome = await _load_pair(pool, decision_id)
    if decision is None or outcome is None:
        return (decision_id, False, 0.0, "missing_pair")
    if (decision.get("classified_sensitivity") or "").lower() == "secret":
        return (decision_id, False, 0.0, "sensitive")
    if not (decision.get("prompt_body") or decision.get("prompt_excerpt")):
        return (decision_id, False, 0.0, "no_prompt")
    rubric, telemetry = await _call_judge(judge_client, cfg, decision, outcome)
    if rubric is None:
        return (decision_id, False, 0.0, "judge_failed")
    await _persist(
        pool, decision_id=decision["decision_id"],
        rubric=rubric, trigger="backfill", telemetry=telemetry, pricing=pricing,
    )
    cost = float(telemetry.get("judge_cost_usd") or 0.0)
    return (decision_id, True, cost, None)


async def main() -> int:
    parser = argparse.ArgumentParser(description="Backfill quality evals on historical decisions")
    parser.add_argument("--apply", action="store_true",
                        help="Actually run the judge (default: dry-run, just counts eligible)")
    parser.add_argument("--limit", type=int, default=None,
                        help="Stop after N evaluations (default: unbounded)")
    parser.add_argument("--concurrency", type=int, default=4,
                        help="Number of parallel judge calls (default: 4)")
    parser.add_argument("--cost-cap", type=float, default=5.0,
                        help="Stop when total cost reaches this in USD (default: $5)")
    args = parser.parse_args()

    _load_env()
    dsn = os.environ.get("NAUTGATE_DB_URL", "postgres://nautgate:nautgate@127.0.0.1:5432/nautgate")

    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=8)
    try:
        ids = await _eligible_ids(pool, args.limit)
        print(f"eligible decisions: {len(ids):,}")
        if not args.apply:
            print("(dry-run; pass --apply to evaluate)")
            return 0
        if not ids:
            print("nothing to do.")
            return 0

        # Pricing + judge client setup, mirroring the live process.
        from pathlib import Path

        import httpx

        from app.pricing import PricingTable
        pricing = PricingTable.from_yaml(
            Path(__file__).resolve().parent.parent / "config" / "pricing.yaml",
        )
        judge_client = httpx.AsyncClient(
            timeout=httpx.Timeout(20.0, connect=3.0),
            limits=httpx.Limits(max_keepalive_connections=8, max_connections=16),
        )

        sem = asyncio.Semaphore(args.concurrency)
        total_cost = 0.0
        succeeded = failed = 0
        started = time.monotonic()
        cap_hit = False

        async def _bound(did):
            nonlocal total_cost, succeeded, failed, cap_hit
            if cap_hit:
                return
            async with sem:
                if cap_hit:
                    return
                res = await _eval_one(pool, pricing, judge_client, did)
                _, ok, cost, err = res
                total_cost += cost
                if ok:
                    succeeded += 1
                else:
                    failed += 1
                if total_cost >= args.cost_cap:
                    cap_hit = True
                done = succeeded + failed
                if done % 25 == 0 or done == len(ids):
                    elapsed = time.monotonic() - started
                    rate = done / elapsed if elapsed else 0
                    eta_min = ((len(ids) - done) / rate / 60) if rate > 0 else 0
                    print(f"  [{done:>5}/{len(ids)}]  ok={succeeded} fail={failed}  "
                          f"cost=${total_cost:.4f}  rate={rate:.1f}/s  eta={eta_min:.1f}min  "
                          f"{'(CAP HIT)' if cap_hit else ''}")

        await asyncio.gather(*(_bound(did) for did in ids))
        await judge_client.aclose()

        elapsed = time.monotonic() - started
        print()
        print("=" * 60)
        print(f"final  succeeded={succeeded}  failed={failed}  "
              f"cost=${total_cost:.4f}  elapsed={elapsed:.0f}s")
        return 0
    finally:
        await pool.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
