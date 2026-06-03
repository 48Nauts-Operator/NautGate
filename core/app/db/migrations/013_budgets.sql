-- Cost budgets. Three scopes covered by one table: per-project, per-agent
-- (mirrors the existing daily_budget_usd on api_keys but with proper
-- enforcement), and per-model-family (catches "the auto tier kept routing
-- to Sonnet" patterns at the source).
--
-- A request is blocked (HTTP 429) when ANY applicable budget is over 100%
-- of cap. A warning chip lights at warn_at_pct (default 80). Warnings are
-- advisory only — they show on the dashboard and in response headers, but
-- don't change routing.

CREATE TABLE IF NOT EXISTS nautgate.budgets (
    scope_type    TEXT         NOT NULL CHECK (scope_type IN ('project', 'agent', 'model_family')),
    scope_id      TEXT         NOT NULL,
    period        TEXT         NOT NULL CHECK (period IN ('daily', 'monthly')),
    cap_usd       NUMERIC(10,2) NOT NULL CHECK (cap_usd >= 0),
    warn_at_pct   NUMERIC(5,2) NOT NULL DEFAULT 80 CHECK (warn_at_pct > 0 AND warn_at_pct <= 100),
    enabled       BOOLEAN      NOT NULL DEFAULT true,
    note          TEXT,
    created_at    TIMESTAMPTZ  NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ  NOT NULL DEFAULT now(),
    PRIMARY KEY (scope_type, scope_id, period)
);

CREATE INDEX IF NOT EXISTS budgets_scope_type_idx ON nautgate.budgets (scope_type);
