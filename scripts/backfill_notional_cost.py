#!/usr/bin/env python3
"""Backfill route_outcomes.notional_cost_usd for rows a pricing gap left NULL.

Notional cost is computed once, at call time, and stored. So adding a missing
model to pricing.yaml only fixes calls from that moment on — every historical
row stays NULL and stays invisible in "Subscription saved". This reprices those
rows with the CURRENT table.

Only touches rows where notional_cost_usd IS NULL and the model now prices.
Rows without usage counts stay NULL (compute_cost returns None) — a missing
price and missing usage are different problems and this only fixes the former.

Rows that were priced with a table we later found WRONG stay wrong on their own
— filling NULLs does not touch them. ``--reprice-all`` recomputes every priced
row too, and reports corrections separately from fills so the two are never
conflated.

  scripts/backfill_notional_cost.py                        # dry run, fills only
  scripts/backfill_notional_cost.py --reprice-all          # dry run, fills + corrects
  scripts/backfill_notional_cost.py --reprice-all --apply  # writes
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
    ap.add_argument(
        "--reprice-all",
        action="store_true",
        help="also recompute rows that already have a cost (fixes stale/wrong rates)",
    )
    args = ap.parse_args()

    settings = get_settings()
    repo_root = pathlib.Path(__file__).resolve().parent.parent
    pricing = PricingTable.from_yaml(repo_root / "config" / "pricing.yaml")

    conn = await asyncpg.connect(settings.nautgate_db_url)
    try:
        where = "actual_model IS NOT NULL" + ("" if args.reprice_all else " AND notional_cost_usd IS NULL")
        rows = await conn.fetch(
            f"""
            SELECT decision_id, actual_provider, actual_model, notional_cost_usd,
                   prompt_tokens, completion_tokens, cache_read_tokens, cache_write_tokens
              FROM nautgate.route_outcomes
             WHERE {where}
            """
        )
        print(f"{len(rows)} rows to consider ({'fill + reprice' if args.reprice_all else 'fill only'})")

        updates: list[tuple[float, str]] = []
        no_price: dict[str, int] = {}
        no_usage = 0
        by_model: dict[str, tuple[int, float]] = {}
        corrections: dict[str, tuple[int, float]] = {}

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
            # numeric column → asyncpg hands back Decimal; compare as float.
            prev = None if r["notional_cost_usd"] is None else float(r["notional_cost_usd"])
            if prev is not None and abs(prev - cost) < 0.000001:
                continue  # already correct — do not rewrite
            updates.append((cost, r["decision_id"]))
            if prev is None:
                n, tot = by_model.get(r["actual_model"], (0, 0.0))
                by_model[r["actual_model"]] = (n + 1, tot + cost)
            else:
                n, delta = corrections.get(r["actual_model"], (0, 0.0))
                corrections[r["actual_model"]] = (n + 1, delta + (cost - prev))

        print(f"\nwould write {len(updates)} rows in total.\n\nnewly priced (were NULL):")
        for m, (n, tot) in sorted(by_model.items(), key=lambda kv: -kv[1][1]):
            print(f"  {m:26s} {n:6d} calls  ${tot:10,.2f}")
        if corrections:
            print("\ncorrections to rows priced with a wrong rate:")
            for m, (n, delta) in sorted(corrections.items(), key=lambda kv: kv[1][1]):
                print(f"  {m:26s} {n:6d} calls  {delta:+12,.2f}")
            print(f"\n  net correction: {sum(d for _, d in corrections.values()):+,.2f}")
        print(f"  newly priced:   ${sum(t for _, t in by_model.values()):,.2f}")
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
