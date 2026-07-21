-- Model test bench: one task fanned out to N models, results side by side.
-- Single table — results are written once when the whole run completes.

CREATE TABLE IF NOT EXISTS nautgate.bench_runs (
    id         UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
    ts         TIMESTAMPTZ  NOT NULL DEFAULT now(),
    agent_id   TEXT         NOT NULL,
    prompt     TEXT         NOT NULL,
    tools      JSONB,                    -- optional OpenAI-format tool defs given to every model
    max_tokens INTEGER      NOT NULL DEFAULT 1000,
    results    JSONB        NOT NULL     -- [{model, provider, status, latency_ms, prompt_tokens,
                                         --   completion_tokens, cost_usd, tool_calls, text, error}]
);

CREATE INDEX IF NOT EXISTS bench_runs_ts_idx ON nautgate.bench_runs (ts DESC);
