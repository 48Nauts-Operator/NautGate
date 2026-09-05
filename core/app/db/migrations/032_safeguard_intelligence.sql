-- Append-only provider safeguard evidence and human/automated reviews.
CREATE TABLE IF NOT EXISTS nautgate.safeguard_events (
    decision_id UUID PRIMARY KEY REFERENCES nautgate.route_decisions(id) ON DELETE CASCADE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    extractor_version TEXT NOT NULL,
    evidence_level TEXT NOT NULL CHECK (
        evidence_level IN ('deterministic', 'provider_confirmed', 'observed', 'inferred', 'insufficient')
    ),
    stop_reason TEXT,
    stop_details JSONB,
    served_model TEXT,
    fallback_blocks JSONB NOT NULL DEFAULT '[]'::jsonb,
    usage_iterations JSONB NOT NULL DEFAULT '[]'::jsonb,
    evidence JSONB NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX IF NOT EXISTS safeguard_events_created_idx
    ON nautgate.safeguard_events(created_at DESC);
CREATE INDEX IF NOT EXISTS safeguard_events_level_idx
    ON nautgate.safeguard_events(evidence_level, created_at DESC);

CREATE TABLE IF NOT EXISTS nautgate.safeguard_reviews (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    decision_id UUID NOT NULL REFERENCES nautgate.safeguard_events(decision_id) ON DELETE CASCADE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    reviewer_type TEXT NOT NULL CHECK (reviewer_type IN ('operator', 'detector')),
    reviewer_id TEXT NOT NULL,
    disposition TEXT NOT NULL CHECK (disposition IN (
        'expected_safeguard', 'likely_false_positive', 'inconsistent_behavior',
        'unexplained_substitution', 'billing_discrepancy', 'needs_investigation',
        'insufficient_evidence'
    )),
    confidence TEXT NOT NULL CHECK (confidence IN ('low', 'medium', 'high')),
    reason_codes TEXT[] NOT NULL DEFAULT '{}',
    notes TEXT,
    detector_version TEXT,
    supersedes UUID REFERENCES nautgate.safeguard_reviews(id)
);
CREATE INDEX IF NOT EXISTS safeguard_reviews_decision_idx
    ON nautgate.safeguard_reviews(decision_id, created_at DESC);

