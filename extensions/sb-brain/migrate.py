"""sb-brain — index migrations.

Idempotent CREATE INDEX IF NOT EXISTS on agents_memory tables. Per Tech
Paper §12.1: the 50ms before_route budget depends on these existing.

The indexes belong to *agents_memory*, not nautgate's own schema. We create
them here because sb-brain is the consumer; if sb-brain is never deployed,
NautGate doesn't need them.
"""

from __future__ import annotations

import logging

import asyncpg

log = logging.getLogger("sb-brain.migrate")

INDEX_SQL = [
    # Per-caller pattern history: WHERE agent_id=$1 AND created_at > NOW()-7d ORDER BY created_at DESC
    """
    CREATE INDEX IF NOT EXISTS idx_memories_agent_recent
        ON memories (agent_id, created_at DESC)
    """,
    # Per-agent project distribution
    """
    CREATE INDEX IF NOT EXISTS idx_coding_usage_agent_date
        ON coding_usage (machine, date DESC)
    """,
]


async def apply(pool: asyncpg.Pool) -> None:
    async with pool.acquire() as conn:
        for sql in INDEX_SQL:
            try:
                await conn.execute(sql)
                log.info("sb_brain_index_applied stmt=%s", sql.strip().split("\n")[0])
            except asyncpg.PostgresError as exc:
                # `coding_usage` may not exist on every install; log and continue.
                log.warning(
                    "sb_brain_index_failed stmt=%s err=%s",
                    sql.strip().split("\n")[0],
                    str(exc),
                )
