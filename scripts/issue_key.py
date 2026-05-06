"""Issue a fresh NautGate API key and insert it into nautgate.api_keys.

Usage:
    cd core && uv run python ../scripts/issue_key.py --agent-id alice
    cd core && uv run python ../scripts/issue_key.py --agent-id ops --profile balanced

Reads NAUTGATE_DB_URL from the environment (or .env in core/). The plaintext token
is printed once on stdout — this is the only time you can see it.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

# Allow running from repo root: import the core/ package.
_CORE = Path(__file__).resolve().parent.parent / "core"
if str(_CORE) not in sys.path:
    sys.path.insert(0, str(_CORE))

import asyncpg  # noqa: E402

from app.auth import issue_key  # noqa: E402


async def main() -> int:
    parser = argparse.ArgumentParser(description="Issue a NautGate API key")
    parser.add_argument("--agent-id", required=True, help="agent_id this key belongs to")
    parser.add_argument(
        "--profile", default="auto", help="default routing profile (default: auto)"
    )
    parser.add_argument(
        "--db-url",
        default=os.environ.get("NAUTGATE_DB_URL"),
        help="Postgres URL (default: $NAUTGATE_DB_URL)",
    )
    args = parser.parse_args()

    if not args.db_url:
        print("error: NAUTGATE_DB_URL not set and --db-url not provided", file=sys.stderr)
        return 2

    plaintext, key_id, key_hash = issue_key()
    conn = await asyncpg.connect(args.db_url)
    try:
        await conn.execute(
            """
            INSERT INTO nautgate.api_keys (id, key_hash, agent_id, default_profile)
            VALUES ($1, $2, $3, $4)
            """,
            key_id,
            key_hash,
            args.agent_id,
            args.profile,
        )
    finally:
        await conn.close()

    print(f"key_id:    {key_id}")
    print(f"agent_id:  {args.agent_id}")
    print(f"profile:   {args.profile}")
    print()
    print("Token (shown ONCE — store it in your client now):")
    print(plaintext)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
