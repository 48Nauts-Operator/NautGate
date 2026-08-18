"""Schema for attestation receipts.

One table. A receipt is what Securosys hands back: a signature over a digest
we chose, made by a key whose private half lives in the HSM. Storing it is the
whole point — the signature is the evidence, and it is worth nothing if it
only ever existed in an HTTP response.
"""

from __future__ import annotations

SCHEMA = """
CREATE SCHEMA IF NOT EXISTS nautgate;

CREATE TABLE IF NOT EXISTS nautgate.attestation (
    id          BIGSERIAL PRIMARY KEY,
    ts          TIMESTAMPTZ NOT NULL DEFAULT now(),
    subject     TEXT        NOT NULL,
    digest      TEXT        NOT NULL,
    key_name    TEXT        NOT NULL,
    algorithm   TEXT        NOT NULL,
    signature   TEXT        NOT NULL,
    tsb_url     TEXT        NOT NULL,
    meta        JSONB       NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS attestation_subject_ts_idx
    ON nautgate.attestation (subject, ts DESC);
"""


async def apply(pool) -> None:
    async with pool.acquire() as conn:
        await conn.execute(SCHEMA)
