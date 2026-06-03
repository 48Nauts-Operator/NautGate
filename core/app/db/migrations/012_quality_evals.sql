-- Quality & Prompt Coach analytics — LLM-as-judge evaluations of completed
-- decisions. One row per evaluated call, FK to route_decisions so a CASCADE
-- delete keeps things tidy. Judge calls themselves are external (direct to
-- OpenAI / LMStudio) and never participate in NautGate's own routing.
--
-- Triggered by quality_eval.process_quality after every outcome that either
-- (a) hits an anomaly condition (was_empty, status>=400, disconnect, bloat
-- spike, etc.) or (b) gets picked by the configured sample rate.

CREATE TABLE IF NOT EXISTS nautgate.quality_evals (
    decision_id      UUID         PRIMARY KEY
                                  REFERENCES nautgate.route_decisions(id) ON DELETE CASCADE,
    ts               TIMESTAMPTZ  NOT NULL DEFAULT now(),
    judge_provider   TEXT         NOT NULL,
    judge_model      TEXT         NOT NULL,
    judge_cost_usd   NUMERIC(10,6),
    judge_latency_ms INTEGER,
    rubric           JSONB,           -- {task_understanding, task_completion, reasoning_efficiency, prompt_clarity}
    failure_tags     TEXT[],          -- {looped, hallucination, off_task, over_thinking, ...}
    suggested_prompt TEXT,
    coach_notes      TEXT,
    trigger          TEXT         NOT NULL,   -- 'sample' | 'anomaly' | 'manual' | 'thumbs_down'
    user_feedback    TEXT                     -- nullable; reserved for Phase 3
);

CREATE INDEX IF NOT EXISTS quality_evals_ts_idx       ON nautgate.quality_evals (ts DESC);
CREATE INDEX IF NOT EXISTS quality_evals_tags_gin     ON nautgate.quality_evals USING GIN (failure_tags);
CREATE INDEX IF NOT EXISTS quality_evals_trigger_idx  ON nautgate.quality_evals (trigger);

-- Seed default quality_eval settings into the existing app_config row.
-- Uses jsonb concat (||) so existing keys (sb_ingest, etc.) are preserved
-- and we only add quality_eval if it's not already there.
UPDATE nautgate.app_config
   SET settings = settings || jsonb_build_object(
        'quality_eval', jsonb_build_object(
            'enabled', true,
            'sample_rate', 0.10,
            'daily_cost_cap_usd', 5.00,
            'judge_provider', 'openrouter',
            'judge_model', 'openai/gpt-4o-mini',
            'judge_base_url', 'https://openrouter.ai/api'
            -- api key is read from env at call time (OPENROUTER_API_KEY for the
            -- default provider; OPENAI_API_KEY for openai; none for lmstudio).
            -- Never stored in the DB.
        )
    )
 WHERE id = 1
   AND NOT (settings ? 'quality_eval');
