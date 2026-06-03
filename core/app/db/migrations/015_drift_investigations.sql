-- Drift Investigator — runs canary suites when drift fires, captures
-- per-call results, generates a verdict from baseline comparison.
--
-- Two tables:
--   drift_investigations — one row per investigation run (suite + verdict)
--   drift_canary_runs    — one row per individual canary call
--
-- An investigation is triggered by either:
--   - The drift_engine when a new alert fires (auto, subject to cooldown +
--     daily budget cap)
--   - A button click on the Drift page UI (manual, always allowed)

CREATE TABLE IF NOT EXISTS nautgate.drift_investigations (
    id              UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
    drift_alert_id  UUID         REFERENCES nautgate.drift_alerts(id) ON DELETE SET NULL,
    provider        TEXT         NOT NULL,
    model           TEXT         NOT NULL,
    metric_name     TEXT,                       -- the drifted metric (NULL = generic)
    canary_suite    TEXT         NOT NULL,      -- 'tokenizer' | 'verbosity' | 'refusal' | 'routing' | 'latency' | 'cross_version'
    triggered_by    TEXT         NOT NULL,      -- 'auto' | 'manual'
    triggered_at    TIMESTAMPTZ  NOT NULL DEFAULT now(),
    completed_at    TIMESTAMPTZ,
    status          TEXT         NOT NULL DEFAULT 'pending',  -- 'pending' | 'running' | 'complete' | 'failed' | 'skipped'
    skip_reason     TEXT,                       -- e.g. 'cooldown', 'daily_budget_exhausted'
    total_cost_usd  NUMERIC(10,6),
    verdict_label   TEXT,                       -- short tag for filtering: 'tokenizer_changed', 'provider_drift', 'inconclusive', …
    verdict_text    TEXT,                       -- human-readable diagnosis
    findings        JSONB                       -- structured per-suite results
);

CREATE INDEX IF NOT EXISTS drift_investigations_alert_idx
    ON nautgate.drift_investigations (drift_alert_id);
CREATE INDEX IF NOT EXISTS drift_investigations_target_idx
    ON nautgate.drift_investigations (provider, model, metric_name, triggered_at DESC);
CREATE INDEX IF NOT EXISTS drift_investigations_recent_idx
    ON nautgate.drift_investigations (triggered_at DESC);


CREATE TABLE IF NOT EXISTS nautgate.drift_canary_runs (
    id                UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
    investigation_id  UUID         NOT NULL REFERENCES nautgate.drift_investigations(id) ON DELETE CASCADE,
    canary_name       TEXT         NOT NULL,    -- 'tokenizer_1kb_lorem', 'verbosity_what_is_two_plus_two', …
    target_provider   TEXT         NOT NULL,
    target_model      TEXT         NOT NULL,
    via              TEXT         NOT NULL,    -- 'openrouter' | 'anthropic-oauth' | 'anthropic-metered' | 'openai-oauth' | 'openai-metered'
    prompt            TEXT,
    prompt_bytes      INTEGER,
    prompt_tokens     INTEGER,
    completion_tokens INTEGER,
    response_text     TEXT,
    response_bytes    INTEGER,
    duration_ms       INTEGER,
    first_byte_ms     INTEGER,
    status_code       INTEGER,
    cost_usd          NUMERIC(10,6),
    error             TEXT,
    ts                TIMESTAMPTZ  NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS drift_canary_runs_investigation_idx
    ON nautgate.drift_canary_runs (investigation_id);


-- Default investigator settings into the app_config row (idempotent).
UPDATE nautgate.app_config
   SET settings = settings || jsonb_build_object(
        'drift_investigator', jsonb_build_object(
            'enabled', true,
            'auto_trigger', true,
            'cooldown_hours', 4,
            'daily_cost_cap_usd', 1.00,
            'prefer_oauth_when_available', true
        )
    )
 WHERE id = 1
   AND NOT (settings ? 'drift_investigator');
