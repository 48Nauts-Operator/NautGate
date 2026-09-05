-- Content-free inspection ledger. A row means the response was examined by the
-- named extractor, whether or not provider-confirmed safeguard evidence existed.
CREATE TABLE IF NOT EXISTS nautgate.safeguard_observations (
    decision_id UUID PRIMARY KEY REFERENCES nautgate.route_decisions(id) ON DELETE CASCADE,
    inspected_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    extractor_version TEXT NOT NULL,
    event_detected BOOLEAN NOT NULL DEFAULT FALSE,
    source TEXT NOT NULL DEFAULT 'live' CHECK (source IN ('live', 'backfill'))
);
CREATE INDEX IF NOT EXISTS safeguard_observations_inspected_idx
    ON nautgate.safeguard_observations(inspected_at DESC);

