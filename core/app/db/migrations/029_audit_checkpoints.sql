-- Gapless transactional allocation plus staged Merkle checkpoints.
-- PostgreSQL sequences are deliberately not used for evidence ordering: a
-- rolled-back nextval() is still consumed and would manufacture a false gap.

CREATE TABLE IF NOT EXISTS nautgate.audit_state (
    singleton       BOOL   PRIMARY KEY DEFAULT TRUE CHECK (singleton),
    next_sequence   BIGINT NOT NULL CHECK (next_sequence > 0)
);

INSERT INTO nautgate.audit_state (singleton, next_sequence)
SELECT TRUE, COALESCE(MAX(evidence_sequence), 0) + 1
  FROM nautgate.audit_receipts
ON CONFLICT (singleton) DO NOTHING;

CREATE TABLE IF NOT EXISTS nautgate.audit_checkpoints (
    checkpoint_id              UUID        PRIMARY KEY,
    schema_version             TEXT        NOT NULL,
    instance_id                TEXT        NOT NULL,
    first_sequence             BIGINT      NOT NULL,
    last_sequence              BIGINT      NOT NULL,
    receipt_count              INT         NOT NULL CHECK (receipt_count > 0),
    merkle_root                BYTEA       NOT NULL CHECK (octet_length(merkle_root) = 32),
    canonical_checkpoint       JSONB       NOT NULL,
    canonical_bytes            BYTEA       NOT NULL,
    checkpoint_hash            BYTEA       NOT NULL CHECK (octet_length(checkpoint_hash) = 32),
    previous_checkpoint_hash   BYTEA       CHECK (
                                          previous_checkpoint_hash IS NULL OR
                                          octet_length(previous_checkpoint_hash) = 32),
    key_id                     TEXT        NOT NULL,
    algorithm                  TEXT        NOT NULL DEFAULT 'SHA256_WITH_RSA',
    signature                  TEXT,
    public_key_fingerprint     TEXT,
    status                     TEXT        NOT NULL DEFAULT 'signing'
                                          CHECK (status IN ('signing', 'verified', 'failed')),
    attempt_count              INT         NOT NULL DEFAULT 0,
    last_error                 TEXT,
    opened_at                  TIMESTAMPTZ NOT NULL,
    closed_at                  TIMESTAMPTZ NOT NULL,
    created_at                 TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    signed_at                  TIMESTAMPTZ,
    UNIQUE (instance_id, first_sequence, last_sequence)
);

CREATE INDEX IF NOT EXISTS idx_audit_checkpoints_status_created
    ON nautgate.audit_checkpoints (status, created_at);
CREATE INDEX IF NOT EXISTS idx_audit_checkpoints_sequence
    ON nautgate.audit_checkpoints (instance_id, last_sequence DESC);

ALTER TABLE nautgate.audit_receipts
    ADD CONSTRAINT fk_audit_receipts_checkpoint
    FOREIGN KEY (checkpoint_id) REFERENCES nautgate.audit_checkpoints(checkpoint_id);

CREATE TABLE IF NOT EXISTS nautgate.audit_gaps (
    id                  BIGSERIAL   PRIMARY KEY,
    expected_sequence   BIGINT      NOT NULL,
    observed_sequence   BIGINT      NOT NULL,
    detected_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    resolved_at         TIMESTAMPTZ,
    resolution          TEXT,
    UNIQUE (expected_sequence, observed_sequence, resolved_at)
);
