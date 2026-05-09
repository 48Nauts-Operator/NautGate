"""sb-privacy — migration for the privacy_log table.

Tamper-evident audit chain (Weaver-shaped) per Tech Paper §13. Each row
carries `prev_hash` (the previous row's `this_hash`) and `this_hash` =
sha256(prev_hash || payload). A consumer can validate the chain end-to-end
by walking the rows in `id` order and checking each hash.

Lives in NautGate's own schema so it joins cleanly with route_decisions.
"""

from __future__ import annotations

import logging

import asyncpg

log = logging.getLogger("sb-privacy.migrate")

CREATE_SQL = """
CREATE TABLE IF NOT EXISTS nautgate.privacy_log (
    id           BIGSERIAL PRIMARY KEY,
    ts           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    decision_id  UUID,
    agent_id     TEXT NOT NULL,
    sensitivity  TEXT NOT NULL,        -- 'pii' | 'secret' | other
    signals      JSONB,                -- which rules fired
    payload_hash TEXT NOT NULL,        -- sha256 of body excerpt at capture
    prev_hash    TEXT NOT NULL,        -- previous row's this_hash; '0' * 64 for the genesis row
    this_hash    TEXT NOT NULL,        -- sha256(prev_hash || payload_hash || ts || decision_id || agent_id || sensitivity)
    UNIQUE (this_hash)
);

CREATE INDEX IF NOT EXISTS idx_privacy_log_ts ON nautgate.privacy_log (ts DESC);
CREATE INDEX IF NOT EXISTS idx_privacy_log_agent ON nautgate.privacy_log (agent_id, ts DESC);
"""


async def apply(pool: asyncpg.Pool) -> None:
    async with pool.acquire() as conn:
        try:
            await conn.execute(CREATE_SQL)
            log.info("sb_privacy_migration_applied")
        except asyncpg.PostgresError as exc:
            log.warning("sb_privacy_migration_failed err=%s", exc)
            raise
