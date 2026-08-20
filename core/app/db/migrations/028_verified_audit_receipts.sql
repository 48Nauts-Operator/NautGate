-- Verified Audit Trail v1: one canonical receipt and durable outbox item per
-- completed route outcome. The HSM checkpoint worker consumes the outbox in
-- monotonic evidence_sequence order.

CREATE SEQUENCE IF NOT EXISTS nautgate.audit_receipt_sequence AS BIGINT START 1;

CREATE TABLE IF NOT EXISTS nautgate.audit_receipts (
    receipt_id          UUID        PRIMARY KEY,
    decision_id         UUID        NOT NULL UNIQUE
                                    REFERENCES nautgate.route_decisions(id) ON DELETE CASCADE,
    evidence_sequence   BIGINT      NOT NULL UNIQUE,
    schema_version      TEXT        NOT NULL,
    canonical_receipt   JSONB       NOT NULL,
    canonical_bytes     BYTEA       NOT NULL,
    receipt_hash        BYTEA       NOT NULL CHECK (octet_length(receipt_hash) = 32),
    status              TEXT        NOT NULL DEFAULT 'pending'
                                    CHECK (status IN ('pending', 'batched', 'verified', 'failed', 'gap')),
    checkpoint_id       UUID,
    merkle_leaf_index   INT,
    merkle_proof        JSONB,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_audit_receipts_status_sequence
    ON nautgate.audit_receipts (status, evidence_sequence);
CREATE INDEX IF NOT EXISTS idx_audit_receipts_created_at
    ON nautgate.audit_receipts (created_at DESC);

CREATE TABLE IF NOT EXISTS nautgate.audit_outbox (
    receipt_id          UUID        PRIMARY KEY
                                    REFERENCES nautgate.audit_receipts(receipt_id) ON DELETE CASCADE,
    evidence_sequence   BIGINT      NOT NULL UNIQUE,
    available_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    claimed_at          TIMESTAMPTZ,
    worker_id           TEXT,
    attempt_count       INT         NOT NULL DEFAULT 0,
    last_error          TEXT
);

CREATE INDEX IF NOT EXISTS idx_audit_outbox_available
    ON nautgate.audit_outbox (available_at, evidence_sequence)
    WHERE claimed_at IS NULL;

