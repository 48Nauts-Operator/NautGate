-- Behavior-drift detection. Catches silent provider-side changes that don't
-- show up as errors but matter operationally:
--   - history compaction (messages_count drops sharply mid-conversation)
--   - tokenization changes (input_tokens / request_size_bytes shifts)
--   - latency regressions (first_byte_ms / duration_ms shifts)
--   - response-shape changes (response_size_bytes for similar inputs shifts)
--   - cache behavior changes (input_tokens for repeated prompts changes)
--
-- Per (provider, model, metric) we keep an EWMA of mean + variance. Each new
-- sample's z-score gets computed against that baseline. |z| > 3 → write an
-- anomaly row. N consecutive anomalies → raise an alert.

-- 1) session_id heuristic — hash of agent_id + first user message snippet,
--    so we can detect compaction (a drop in messages_count between consecutive
--    turns of the same conversation).
ALTER TABLE nautgate.route_decisions
    ADD COLUMN IF NOT EXISTS session_id TEXT;

CREATE INDEX IF NOT EXISTS route_decisions_session_ts_idx
    ON nautgate.route_decisions (session_id, ts DESC)
    WHERE session_id IS NOT NULL;

-- 2) Rolling per-(provider, model, metric) baseline using EWMA.
--    ewma_mean and ewma_variance updated on every sample. sample_count tracks
--    how many observations we've seen (used to skip drift checks on cold metrics).
CREATE TABLE IF NOT EXISTS nautgate.model_baselines (
    provider        TEXT        NOT NULL,
    model           TEXT        NOT NULL,
    metric_name     TEXT        NOT NULL,
    ewma_mean       DOUBLE PRECISION NOT NULL DEFAULT 0,
    ewma_variance   DOUBLE PRECISION NOT NULL DEFAULT 0,
    sample_count    INTEGER     NOT NULL DEFAULT 0,
    consecutive_anomalies INTEGER NOT NULL DEFAULT 0,
    last_observed   DOUBLE PRECISION,
    last_z_score    DOUBLE PRECISION,
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (provider, model, metric_name)
);

CREATE INDEX IF NOT EXISTS model_baselines_updated_idx
    ON nautgate.model_baselines (updated_at DESC);

-- 3) Anomaly events (append-only). One row per outlier observation.
CREATE TABLE IF NOT EXISTS nautgate.model_anomalies (
    id              UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    provider        TEXT        NOT NULL,
    model           TEXT        NOT NULL,
    metric_name     TEXT        NOT NULL,
    z_score         DOUBLE PRECISION NOT NULL,
    observed_value  DOUBLE PRECISION NOT NULL,
    baseline_mean   DOUBLE PRECISION NOT NULL,
    baseline_stddev DOUBLE PRECISION NOT NULL,
    decision_id     UUID        REFERENCES nautgate.route_decisions(id) ON DELETE CASCADE,
    ts              TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS model_anomalies_model_metric_ts_idx
    ON nautgate.model_anomalies (provider, model, metric_name, ts DESC);

CREATE INDEX IF NOT EXISTS model_anomalies_ts_idx
    ON nautgate.model_anomalies (ts DESC);

-- 4) Drift alerts — raised when N consecutive anomalies cluster on the same
--    (provider, model, metric). One row per cluster, with started_at and
--    resolved_at; resolved_at is set when baseline restabilizes.
CREATE TABLE IF NOT EXISTS nautgate.drift_alerts (
    id              UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    provider        TEXT        NOT NULL,
    model           TEXT        NOT NULL,
    metric_name     TEXT        NOT NULL,
    direction       TEXT        NOT NULL,   -- 'up' | 'down'
    started_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    resolved_at     TIMESTAMPTZ,
    peak_z_score    DOUBLE PRECISION NOT NULL,
    peak_observed   DOUBLE PRECISION NOT NULL,
    baseline_at_alert DOUBLE PRECISION NOT NULL,
    sample_count    INTEGER     NOT NULL DEFAULT 1
);

CREATE INDEX IF NOT EXISTS drift_alerts_open_idx
    ON nautgate.drift_alerts (provider, model, metric_name)
    WHERE resolved_at IS NULL;

CREATE INDEX IF NOT EXISTS drift_alerts_ts_idx
    ON nautgate.drift_alerts (started_at DESC);
