-- Champion–challenger shadow testing. A sampled % of real (tool-free,
-- non-sensitive) traffic is mirrored to a cheaper challenger model in the
-- background; a blind judge picks the better answer. One row per paired trial.
-- Nothing here ever touches the user-facing response path.

CREATE TABLE IF NOT EXISTS nautgate.shadow_trials (
    id                    UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
    decision_id           UUID         NOT NULL
                                       REFERENCES nautgate.route_decisions(id) ON DELETE CASCADE,
    ts                    TIMESTAMPTZ  NOT NULL DEFAULT now(),
    champion_provider     TEXT         NOT NULL,
    champion_model        TEXT         NOT NULL,
    challenger_provider   TEXT         NOT NULL,
    challenger_model      TEXT         NOT NULL,
    challenger_response   TEXT,                    -- challenger's full answer (never shown to the user)
    challenger_status     INTEGER,                 -- HTTP status of the challenger call
    challenger_latency_ms INTEGER,
    challenger_cost_usd   NUMERIC(10,6),
    champion_cost_usd     NUMERIC(10,6),           -- notional cost of the champion call, for the ratio
    verdict               TEXT,                    -- 'champion' | 'challenger' | 'tie' | 'error'
    judge_reason          TEXT,
    judge_cost_usd        NUMERIC(10,6)
);

CREATE INDEX IF NOT EXISTS shadow_trials_ts_idx   ON nautgate.shadow_trials (ts DESC);
CREATE INDEX IF NOT EXISTS shadow_trials_pair_idx ON nautgate.shadow_trials (champion_model, challenger_model);

-- Seed default shadow settings (disabled — flipped on from the Insights page).
UPDATE nautgate.app_config
   SET settings = settings || jsonb_build_object(
        'shadow', jsonb_build_object(
            'enabled', false,
            'sample_rate', 0.10,
            'challenger_provider', 'openrouter',
            'challenger_model', 'openrouter/openai/gpt-4o-mini',
            'daily_cost_cap_usd', 2.00,
            'max_prompt_bytes', 131072
        )
    )
 WHERE id = 1
   AND NOT (settings ? 'shadow');
