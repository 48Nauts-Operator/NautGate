-- Durable Max Guard state and concurrency-safe preflight reservations.
CREATE TABLE IF NOT EXISTS nautgate.max_guard_sessions (
    identity TEXT PRIMARY KEY,
    app TEXT,
    project_id TEXT,
    native_session TEXT,
    fresh_tokens BIGINT NOT NULL DEFAULT 0,
    cache_read_tokens BIGINT NOT NULL DEFAULT 0,
    cache_write_tokens BIGINT NOT NULL DEFAULT 0,
    output_tokens BIGINT NOT NULL DEFAULT 0,
    request_count BIGINT NOT NULL DEFAULT 0,
    paused BOOLEAN NOT NULL DEFAULT FALSE,
    pause_reason TEXT,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS nautgate.max_guard_reservations (
    id UUID PRIMARY KEY,
    identity TEXT NOT NULL REFERENCES nautgate.max_guard_sessions(identity) ON DELETE CASCADE,
    project_id TEXT,
    estimated_fresh_tokens BIGINT NOT NULL,
    actual_fresh_tokens BIGINT,
    cache_read_tokens BIGINT,
    cache_write_tokens BIGINT,
    output_tokens BIGINT,
    status TEXT NOT NULL DEFAULT 'reserved'
        CHECK (status IN ('reserved', 'reconciled', 'released')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    reconciled_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS max_guard_reservations_identity_time_idx
    ON nautgate.max_guard_reservations(identity, created_at DESC);
CREATE INDEX IF NOT EXISTS max_guard_reservations_project_time_idx
    ON nautgate.max_guard_reservations(project_id, created_at DESC);
CREATE INDEX IF NOT EXISTS max_guard_reservations_time_idx
    ON nautgate.max_guard_reservations(created_at DESC);

CREATE TABLE IF NOT EXISTS nautgate.max_guard_overrides (
    id UUID PRIMARY KEY,
    identity TEXT NOT NULL REFERENCES nautgate.max_guard_sessions(identity) ON DELETE CASCADE,
    extra_tokens BIGINT NOT NULL DEFAULT 0,
    remaining_requests INT,
    reason TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at TIMESTAMPTZ NOT NULL,
    CHECK (remaining_requests IS NULL OR remaining_requests >= 0)
);
CREATE INDEX IF NOT EXISTS max_guard_overrides_active_idx
    ON nautgate.max_guard_overrides(identity, expires_at DESC);

CREATE TABLE IF NOT EXISTS nautgate.max_guard_control_events (
    id UUID PRIMARY KEY,
    actor_agent_id TEXT NOT NULL,
    identity TEXT NOT NULL REFERENCES nautgate.max_guard_sessions(identity) ON DELETE CASCADE,
    action TEXT NOT NULL CHECK (action IN ('pause', 'resume', 'authorize')),
    details JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS max_guard_control_events_identity_time_idx
    ON nautgate.max_guard_control_events(identity, created_at DESC);

ALTER TABLE nautgate.audit_receipts ALTER COLUMN decision_id DROP NOT NULL;
ALTER TABLE nautgate.audit_receipts
    ADD COLUMN IF NOT EXISTS guard_event_id UUID UNIQUE
        REFERENCES nautgate.max_guard_control_events(id) ON DELETE CASCADE;
ALTER TABLE nautgate.audit_receipts
    DROP CONSTRAINT IF EXISTS audit_receipts_exactly_one_source;
ALTER TABLE nautgate.audit_receipts
    ADD CONSTRAINT audit_receipts_exactly_one_source CHECK (
        (decision_id IS NOT NULL)::int + (guard_event_id IS NOT NULL)::int = 1
    );
