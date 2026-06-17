-- LLM-Probing — proactive provenance & degradation monitoring for the
-- max-subscription paths (Claude Max / ChatGPT) vs their metered twins.
--
-- A scheduled cycle fires a fixed probe suite at each configured model on BOTH
-- the subscription transport (OAuth) and a metered transport (OpenRouter / API
-- key), records a fingerprint per leg, baselines it, and raises alerts on:
--   model_mismatch        — provider returned a different `model` than requested
--   tokenizer_shift       — input_tokens/byte moved vs the model's own baseline
--   latency_spike         — TTFT moved vs baseline
--   quality_drop          — judge score moved vs baseline
--   cross_path_divergence — subscription leg differs from the metered leg
--   refusal               — probe was refused / came back empty
--   auth_expired          — the subscription OAuth token is missing/expired
--
-- We cannot PROVE which weights ran (no client-side attestation); these are
-- statistical signals that something changed, surfaced for the operator.

CREATE TABLE IF NOT EXISTS nautgate.llm_probe_config (
    id             INTEGER     PRIMARY KEY DEFAULT 1,
    enabled        BOOLEAN     NOT NULL DEFAULT false,
    interval_hours INTEGER     NOT NULL DEFAULT 6,
    targets        TEXT[]      NOT NULL DEFAULT '{}',   -- "provider/model" entries
    last_run_at    TIMESTAMPTZ,
    next_run_at    TIMESTAMPTZ,
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (id = 1)
);
INSERT INTO nautgate.llm_probe_config (id, enabled, interval_hours, targets)
VALUES (1, false, 6, '{}')
ON CONFLICT (id) DO NOTHING;

CREATE TABLE IF NOT EXISTS nautgate.llm_probe_runs (
    id                UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    cycle_id          UUID        NOT NULL,
    ts                TIMESTAMPTZ NOT NULL DEFAULT now(),
    probe_name        TEXT        NOT NULL,
    provider          TEXT        NOT NULL,   -- requested provider (e.g. anthropic)
    model             TEXT        NOT NULL,   -- requested model
    via               TEXT        NOT NULL,   -- transport leg (anthropic-oauth | openrouter | …)
    observed_model    TEXT,                   -- the `model` string the provider returned
    prompt_bytes      INTEGER,
    prompt_tokens     INTEGER,
    completion_tokens INTEGER,
    tokens_per_byte   NUMERIC(8,5),
    response_sha      TEXT,                   -- sha1 of normalized response (weak determinism signal)
    response_text     TEXT,
    first_byte_ms     INTEGER,
    duration_ms       INTEGER,
    status_code       INTEGER,
    quality_score     NUMERIC(3,1),           -- judge rubric task_completion, when scored
    refused           BOOLEAN     NOT NULL DEFAULT false,
    cost_usd          NUMERIC(10,6),
    error             TEXT
);
CREATE INDEX IF NOT EXISTS llm_probe_runs_model_ts_idx
    ON nautgate.llm_probe_runs (provider, model, ts DESC);
CREATE INDEX IF NOT EXISTS llm_probe_runs_cycle_idx
    ON nautgate.llm_probe_runs (cycle_id);

-- Self-drift baselines, one row per (provider, via, model, metric). Mirrors
-- nautgate.model_baselines but keyed by transport leg so the OAuth path and the
-- metered path each get their own baseline.
CREATE TABLE IF NOT EXISTS nautgate.llm_probe_baselines (
    provider             TEXT        NOT NULL,
    via                  TEXT        NOT NULL,
    model                TEXT        NOT NULL,
    metric               TEXT        NOT NULL,   -- tokens_per_byte | first_byte_ms | quality_score
    ewma_mean            DOUBLE PRECISION NOT NULL,
    ewma_variance        DOUBLE PRECISION NOT NULL DEFAULT 0,
    sample_count         INTEGER     NOT NULL DEFAULT 0,
    consecutive_anomalies INTEGER    NOT NULL DEFAULT 0,
    last_observed        DOUBLE PRECISION,
    last_z_score         DOUBLE PRECISION,
    updated_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (provider, via, model, metric)
);

CREATE TABLE IF NOT EXISTS nautgate.llm_probe_alerts (
    id         UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    cycle_id   UUID,
    ts         TIMESTAMPTZ NOT NULL DEFAULT now(),
    provider   TEXT        NOT NULL,
    model      TEXT        NOT NULL,
    alert_type TEXT        NOT NULL,
    severity   TEXT        NOT NULL DEFAULT 'warning',  -- info | warning | critical
    detail     JSONB,
    resolved_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS llm_probe_alerts_ts_idx
    ON nautgate.llm_probe_alerts (ts DESC);
CREATE INDEX IF NOT EXISTS llm_probe_alerts_open_idx
    ON nautgate.llm_probe_alerts (provider, model) WHERE resolved_at IS NULL;
