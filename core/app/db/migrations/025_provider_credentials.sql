-- Provider API keys stored encrypted at rest (NAUTGATE-8).
-- The ciphertext is AES-256-GCM; the key is derived from NAUTGATE_MASTER_KEY
-- (env, outside the DB) so a leaked dump alone cannot decrypt these. Issued
-- ng_ keys stay argon2-hashed in api_keys — different problem, different table.
CREATE TABLE IF NOT EXISTS nautgate.provider_credentials (
    provider     TEXT        PRIMARY KEY,          -- 'openrouter' | 'anthropic' | 'openai' | 'gemini'
    ciphertext   BYTEA       NOT NULL,
    nonce        BYTEA       NOT NULL,
    last4        TEXT        NOT NULL,              -- for display only
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
