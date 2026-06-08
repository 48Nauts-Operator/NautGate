-- 017_behavioral_canary.sql — apples-to-apples model comparison runs.
--
-- One row per (canary prompt × model × run). Each comparison run gets a
-- fresh comparison_id so the dashboard can show all results from one
-- "Run comparison now" click side-by-side. Quality rubric scores are
-- stored inline so the comparison view doesn't have to join through
-- quality_evals (canary runs don't have route_decisions rows).

CREATE TABLE IF NOT EXISTS nautgate.behavioral_canary_runs (
    id              uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    comparison_id   uuid        NOT NULL,
    ts              timestamptz NOT NULL DEFAULT now(),
    canary_name     text        NOT NULL,   -- e.g. "read_first", "respect_constraint"
    prompt          text        NOT NULL,
    target_provider text        NOT NULL,   -- always "openrouter" today
    target_model    text        NOT NULL,   -- e.g. "anthropic/claude-opus-4-7"
    response_text   text,
    tool_calls_made jsonb,                  -- if tools were offered + chosen
    prompt_tokens   integer,
    completion_tokens integer,
    duration_ms     integer,
    status_code     integer,
    error           text,
    -- Judge scores (run via existing quality_eval pipeline, stored inline).
    rubric          jsonb,                  -- {task_understanding, task_completion,
                                            --  reasoning_efficiency, action_compliance,
                                            --  prompt_clarity}
    failure_tags    text[],
    coach_notes     text,
    judge_cost_usd  numeric(10,6)
);

CREATE INDEX IF NOT EXISTS behavioral_canary_runs_comp_idx
    ON nautgate.behavioral_canary_runs (comparison_id);
CREATE INDEX IF NOT EXISTS behavioral_canary_runs_ts_idx
    ON nautgate.behavioral_canary_runs (ts DESC);
CREATE INDEX IF NOT EXISTS behavioral_canary_runs_model_idx
    ON nautgate.behavioral_canary_runs (target_model);
