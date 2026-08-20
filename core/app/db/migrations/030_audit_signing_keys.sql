-- Public trust anchors retained across signing-key rotation. Private keys never
-- enter NautGate or this table; they remain inside the TSB/HSM boundary.

CREATE TABLE IF NOT EXISTS nautgate.audit_signing_keys (
    key_id                   TEXT        PRIMARY KEY,
    purpose                  TEXT        NOT NULL DEFAULT 'nautgate-audit-checkpoint-v1'
                                         CHECK (purpose = 'nautgate-audit-checkpoint-v1'),
    algorithm                TEXT        NOT NULL DEFAULT 'SHA256_WITH_RSA'
                                         CHECK (algorithm = 'SHA256_WITH_RSA'),
    public_key_fingerprint   TEXT        NOT NULL CHECK (
                                         public_key_fingerprint ~ '^[0-9a-f]{64}$'),
    public_key_pem           TEXT        NOT NULL,
    status                   TEXT        NOT NULL DEFAULT 'active'
                                         CHECK (status IN ('active', 'retired', 'revoked')),
    valid_from               TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    valid_until              TIMESTAMPTZ,
    created_at               TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
