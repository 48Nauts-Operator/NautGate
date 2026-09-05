"""Inspect retained historical responses for structured safeguard evidence.

Dry-run is the default and performs no writes. It prints aggregate counts only;
response content is never printed or copied.

Usage (from core/):
    uv run python ../scripts/safeguard_backfill.py
    uv run python ../scripts/safeguard_backfill.py --limit 500
    uv run python ../scripts/safeguard_backfill.py --apply
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

_CORE = Path(__file__).resolve().parent.parent / "core"
if str(_CORE) not in sys.path:
    sys.path.insert(0, str(_CORE))

import asyncpg  # noqa: E402

from app.safeguard import EXTRACTOR_VERSION, extract_safeguard_evidence  # noqa: E402


def _load_env() -> None:
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    for path in (_CORE / ".env", _CORE.parent / ".env", _CORE.parent / "deploy" / ".env"):
        if path.is_file():
            load_dotenv(path, override=False)


def response_payloads(body: str) -> list[dict]:
    """Decode JSON or SSE into extractor inputs without retaining text."""
    try:
        value = json.loads(body)
        return [value] if isinstance(value, dict) else []
    except (json.JSONDecodeError, TypeError):
        pass
    payloads = []
    for line in body.splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        value = line[5:].strip()
        if not value or value == "[DONE]":
            continue
        try:
            item = json.loads(value)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict):
            payloads.append(item)
    return payloads


async def main() -> int:
    parser = argparse.ArgumentParser(description="Backfill content-free safeguard observations")
    parser.add_argument("--apply", action="store_true", help="Write observations/events")
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args()
    _load_env()
    dsn = os.environ.get("NAUTGATE_DB_URL", "postgres://nautgate:nautgate@127.0.0.1:5432/nautgate")
    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=2)
    try:
        observations_exist = bool(
            await pool.fetchval("SELECT to_regclass('nautgate.safeguard_observations')")
        )
        sql = """
            SELECT d.id, o.response_body
              FROM nautgate.route_decisions d
              JOIN nautgate.route_outcomes o ON o.decision_id = d.id
             WHERE o.response_body IS NOT NULL
        """
        if observations_exist:
            sql += """
               AND NOT EXISTS (
                   SELECT 1 FROM nautgate.safeguard_observations so
                    WHERE so.decision_id = d.id
               )
            """
        sql += """
             ORDER BY d.ts
        """
        if args.limit:
            sql += f" LIMIT {max(1, int(args.limit))}"
        eligible = decoded = confirmed = undecodable = written = 0
        reader = await pool.acquire()
        writer = await pool.acquire() if args.apply else None
        writer_tx = writer.transaction() if writer else None
        if writer_tx:
            await writer_tx.start()
        try:
            async with reader.transaction():
                async for row in reader.cursor(sql, prefetch=5):
                    eligible += 1
                    payloads = response_payloads(row["response_body"])
                    if not payloads:
                        undecodable += 1
                        continue
                    decoded += 1
                    evidence = extract_safeguard_evidence(payloads)
                    confirmed += int(bool(evidence))
                    if not writer:
                        continue
                    await writer.execute(
                        """
                        INSERT INTO nautgate.safeguard_observations
                            (decision_id, extractor_version, event_detected, source)
                        VALUES ($1, $2, $3, 'backfill')
                        ON CONFLICT (decision_id) DO NOTHING
                        """,
                        row["id"],
                        EXTRACTOR_VERSION,
                        bool(evidence),
                    )
                    written += 1
                    if evidence:
                        await writer.execute(
                            """
                            INSERT INTO nautgate.safeguard_events
                                (decision_id, extractor_version, evidence_level, stop_reason,
                                 stop_details, served_model, fallback_blocks, usage_iterations, evidence)
                            VALUES ($1,$2,$3,$4,$5::jsonb,$6,$7::jsonb,$8::jsonb,$9::jsonb)
                            ON CONFLICT (decision_id) DO NOTHING
                            """,
                            row["id"],
                            evidence["extractor_version"],
                            evidence["evidence_level"],
                            evidence.get("stop_reason"),
                            json.dumps(evidence.get("stop_details")),
                            evidence.get("served_model"),
                            json.dumps(evidence.get("fallback_blocks") or []),
                            json.dumps(evidence.get("usage_iterations") or []),
                            json.dumps(evidence),
                        )
            if writer_tx:
                await writer_tx.commit()
        except BaseException:
            if writer_tx:
                await writer_tx.rollback()
            raise
        finally:
            await pool.release(reader)
            if writer:
                await pool.release(writer)

        print(f"eligible retained responses: {eligible:,}")
        print(f"decoded and inspectable:     {decoded:,}")
        print(f"undecodable/incomplete:      {undecodable:,}")
        print(f"provider-confirmed events:   {confirmed:,}")
        if not args.apply:
            print("dry-run complete; no database rows written")
            return 0

        print(f"wrote {written:,} observations and up to {confirmed:,} events")
        return 0
    finally:
        await pool.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
