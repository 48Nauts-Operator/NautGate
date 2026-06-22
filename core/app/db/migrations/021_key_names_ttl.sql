-- Key management from the dashboard: human-readable name, optional TTL/expiry,
-- and soft revocation. All nullable so existing keys keep working unchanged
-- (NULL expires_at = never expires, NULL revoked_at = active).
ALTER TABLE nautgate.api_keys
    ADD COLUMN IF NOT EXISTS name       TEXT,
    ADD COLUMN IF NOT EXISTS expires_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS revoked_at TIMESTAMPTZ;
