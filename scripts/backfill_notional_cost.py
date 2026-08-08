#!/usr/bin/env python3
"""Backfill route_outcomes.notional_cost_usd for rows a pricing gap left NULL.

Notional cost is computed once, at call time, and stored. So adding a missing
model to pricing.yaml only fixes calls from that moment on — every historical
row stays NULL and stays invisible in "Subscription saved". This reprices those
rows with the CURRENT table.

Only touches rows where notional_cost_usd IS NULL and the model now prices.
Rows without usage counts stay NULL (compute_cost returns None) — a missing
price and missing usage are different problems and this only fixes the former.

  scripts/backfill_notional_cost.py            # dry run — reports, writes nothing
  scripts/backfill_notional_cost.py --apply    # writes
"""

from __future__ import annotations

import argparse
import asyncio
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "core"))

import asyncpg  # noqa: E402

from app.pricing import PricingTable  # noqa: E402
from app.settings import get_settings  # noqa: E402

# The OAuth forwarder prices subscription traffic as provider "anthropic"
# regardless of the lane it arrived on, so repricing must use the same key.
_PROVIDER_ALIASES = {"anthropic-oauth": "anthropic"}


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="write (default is a dry run)")
    args = ap.parse_args()

    settings = get_settings()
    repo_root = pathlib.Path(__file__).resolve().parent.parent
    pricing = PricingTable.from_yaml(repo_root / "config" / "pricing.yaml")

    conn = await asyncpg.connect(settings.nautgate_db_url)
    try:
        rows = await conn.fetch(
            """
            SELECT decision_id, actual_provider, actual_model,
                   prompt_tokens, completion_tokens, cache_read_tokens, cache_write_tokens
              FROM nautgate.route_outcomes
             WHERE notional_cost_usd IS NULL AND actual_model IS NOT NULL
            """
        )
        print(f"{len(rows)} unpriced rows to consider")

        updates: list[tuple[float, str]] = []
        no_price: dict[str, int] = {}
        no_usage = 0
        by_model: dict[str, tuple[int, float]] = {}

        for r in rows:
            provider = _PROVIDER_ALIASES.get(r["actual_provider"], r["actual_provider"])
            cost = pricing.compute_cost(
                provider,
                r["actual_model"],
                prompt_tokens=r["prompt_tokens"],
                completion_tokens=r["completion_tokens"],
                cache_read_tokens=r["cache_read_tokens"],
                cache_write_tokens=r["cache_write_tokens"],
            )
            if cost is None:
                if pricing.lookup(provider, r["actual_model"]) is None:
                    no_price[f"{provider}/{r['actual_model']}"] = (
                        no_price.get(f"{provider}/{r['actual_model']}", 0) + 1
                    )
                else:
                    no_usage += 1
                continue
            updates.append((cost, r["decision_id"]))
            n, tot = by_model.get(r["actual_model"], (0, 0.0))
            by_model[r["actual_model"]] = (n + 1, tot + cost)

        print(f"\nwould price {len(updates)} rows:")
        for m, (n, tot) in sorted(by_model.items(), key=lambda kv: -kv[1][1]):
            print(f"  {m:26s} {n:6d} calls  ${tot:10,.2f}")
        print(f"\n  total recovered: ${sum(c for c, _ in updates):,.2f}")
        print(f"  skipped, no usage recorded: {no_usage}")
        if no_price:
            print("  skipped, still no pricing entry:")
            for k, n in sorted(no_price.items(), key=lambda kv: -kv[1]):
                print(f"    {k:34s} {n:6d}")

        if not args.apply:
            print("\nDRY RUN — nothing written. Re-run with --apply to write.")
            return 0

        await conn.executemany(
            "UPDATE nautgate.route_outcomes SET notional_cost_usd = $1 WHERE decision_id = $2",
            updates,
        )
        print(f"\nwrote {len(updates)} rows.")
        return 0
    finally:
        await conn.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
