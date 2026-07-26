-- Compliance AUDIT trace (NAUTGATE-25).
--
-- One row per call, recorded alongside the decision. NautGate is the audit layer
-- for compliance, not the compliance layer: nothing here gates a request. The
-- trace says what a call touched so an operator can go back later and see which
-- rules it may have tapped into.
--
-- Kept in its own table rather than as columns on route_decisions so the audit
-- path stays untouched when the trace is disabled, and so a re-evaluation can
-- rewrite traces without rewriting the decision history they point at.
CREATE TABLE IF NOT EXISTS nautgate.compliance_traces (
    decision_id       UUID        PRIMARY KEY REFERENCES nautgate.route_decisions(id) ON DELETE CASCADE,
    ts                TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    activity          TEXT        NOT NULL,
    label             TEXT        NOT NULL,          -- G | Y | O | R | X  (severity reading, not a gate)
    confidence        TEXT        NOT NULL,          -- declared | inferred | fallback
    effect            TEXT,                          -- draft | recommendation | ranking | significant | execution
    data_class        TEXT        NOT NULL,          -- public | personal | sensitive | secret
    evaluated_against TEXT[]      NOT NULL DEFAULT '{}',  -- the jurisdiction lens, so past readings stay auditable
    regimes_touched   TEXT[]      NOT NULL DEFAULT '{}',
    destination       JSONB       NOT NULL DEFAULT '{}'::jsonb,
    provider_terms    JSONB       NOT NULL DEFAULT '{}'::jsonb,
    flags             JSONB       NOT NULL DEFAULT '[]'::jsonb,
    flag_count        INT         NOT NULL DEFAULT 0,
    -- Set when a human has looked at the flags. Never set by the engine.
    reviewed_at       TIMESTAMPTZ,
    reviewed_by       TEXT
);

-- The three questions the Compliance page asks: what is flagged, what left the
-- home region, and what touched personal data.
CREATE INDEX IF NOT EXISTS idx_ct_flagged
    ON nautgate.compliance_traces (ts DESC) WHERE flag_count > 0;
CREATE INDEX IF NOT EXISTS idx_ct_ts
    ON nautgate.compliance_traces (ts DESC);
CREATE INDEX IF NOT EXISTS idx_ct_label
    ON nautgate.compliance_traces (label, ts DESC);
CREATE INDEX IF NOT EXISTS idx_ct_transfer
    ON nautgate.compliance_traces (ts DESC)
    WHERE (destination->>'third_country_transfer') = 'true';
