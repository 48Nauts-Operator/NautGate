"""Backfill nautgate.route_outcomes.cost_usd for rows that have token counts
but a NULL cost — historically the result of two bugs:

  1. decision_provider="passthrough" never matched pricing.yaml keys
     (which are namespaced anthropic/* / openai/* / etc.)
  2. Snapshot model IDs (claude-opus-4-7, claude-sonnet-4-6, …) had no
     pricing entry — only the family base name (claude-opus-4) did.

Both are fixed in live code now. This script applies the same resolution
to historical rows so the dashboard total reflects real spend.

Usage:
    cd core && uv run python ../scripts/backfill_cost_usd.py            # dry-run
    cd core && uv run python ../scripts/backfill_cost_usd.py --apply    # write

Reads NAUTGATE_DB_URL from the environment (or core/.env).
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

_CORE = Path(__file__).resolve().parent.parent / "core"
if str(_CORE) not in sys.path:
    sys.path.insert(0, str(_CORE))

import asyncpg  # noqa: E402

from app.pricing import PricingTable  # noqa: E402
from app.routes.v1 import _resolve_pricing_provider  # noqa: E402


def _load_env() -> None:
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    for p in (_CORE / ".env", _CORE.parent / ".env"):
        if p.is_file():
            load_dotenv(p, override=False)
            break


async def main() -> int:
    parser = argparse.ArgumentParser(description="Backfill NULL cost_usd rows")
    parser.add_argument("--apply", action="store_true",
                        help="Actually UPDATE rows (default: dry-run)")
    parser.add_argument("--limit", type=int, default=None,
                        help="Stop after N updates (default: no limit)")
    args = parser.parse_args()

    _load_env()
    dsn = os.environ.get("NAUTGATE_DB_URL")
    if not dsn:
        print("NAUTGATE_DB_URL not set", file=sys.stderr)
        return 2

    pricing_path = _CORE.parent / "config" / "pricing.yaml"
    pricing = PricingTable.from_yaml(pricing_path)
    print(f"loaded {pricing.size} pricing entries from {pricing_path}")

    conn = await asyncpg.connect(dsn)
    try:
        rows = await conn.fetch(
            """
            SELECT o.decision_id,
                   d.decision_provider,
                   d.decision_model,
                   o.actual_provider,
                   o.prompt_tokens,
                   o.completion_tokens
              FROM nautgate.route_outcomes o
              JOIN nautgate.route_decisions d ON d.id = o.decision_id
             WHERE o.cost_usd IS NULL
               AND (o.prompt_tokens IS NOT NULL OR o.completion_tokens IS NOT NULL)
               AND (COALESCE(o.prompt_tokens,0) + COALESCE(o.completion_tokens,0)) > 0
             ORDER BY d.ts ASC
            """
        )
        print(f"{len(rows)} candidate rows with NULL cost + non-zero tokens")

        updates: list[tuple[str, float]] = []
        unpriced: dict[tuple[str, str], int] = {}
        total = 0.0
        for r in rows:
            provider = _resolve_pricing_provider(
                r["decision_provider"], r["actual_provider"], r["decision_model"],
            )
            cost = pricing.compute_cost(
                provider, r["decision_model"],
                prompt_tokens=r["prompt_tokens"],
                completion_tokens=r["completion_tokens"],
            )
            if cost is None:
                key = (provider or "?", r["decision_model"] or "?")
                unpriced[key] = unpriced.get(key, 0) + 1
                continue
            updates.append((str(r["decision_id"]), cost))
            total += cost
            if args.limit and len(updates) >= args.limit:
                break

        print(f"\n=== {len(updates)} priceable rows → ${total:.4f} total ===")
        if unpriced:
            print(f"\n{sum(unpriced.values())} rows still unpriced "
                  f"(add to pricing.yaml to recover):")
            for (prov, model), cnt in sorted(unpriced.items(), key=lambda x: -x[1]):
                print(f"  {cnt:>4}  {prov}/{model}")

        if not args.apply:
            print("\n(dry-run; pass --apply to write)")
            return 0
        if not updates:
            print("nothing to apply")
            return 0

        print(f"\napplying {len(updates)} updates …")
        async with conn.transaction():
            await conn.executemany(
                "UPDATE nautgate.route_outcomes SET cost_usd = $2 WHERE decision_id = $1",
                [(__import__("uuid").UUID(did), cost) for did, cost in updates],
            )
        print(f"done. total backfilled: ${total:.4f}")
        return 0
    finally:
        await conn.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
