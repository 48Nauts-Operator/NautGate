-- NautGate schema v1.
-- Source: Tech Paper §10 (Database schema — full DDL) + §11.3 (streaming-cap columns).
-- Apply order: schema → tables (api_keys → route_decisions → route_outcomes → provider_health → routing_preferences).

CREATE SCHEMA IF NOT EXISTS nautgate;

-- ----------------------------------------------------------------------------
-- API keys (caller identity). Bearer tokens hashed with argon2id (Day 4).
-- ----------------------------------------------------------------------------
CREATE TABLE nautgate.api_keys (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    key_hash TEXT NOT NULL UNIQUE,
    agent_id TEXT NOT NULL,
    default_profile TEXT NOT NULL DEFAULT 'auto',
    daily_budget_usd NUMERIC(8, 4),
    enabled_providers TEXT[],
    accumulator_cap_bytes INT DEFAULT 8388608,        -- per-key override of stream-capture cap (Tech Paper §11.3)
    override_model TEXT,                              -- per-key hard model pin (precedence ladder level 3)
    created_at TIMESTAMPTZ DEFAULT NOW(),
    last_used_at TIMESTAMPTZ
);

-- ----------------------------------------------------------------------------
-- Per-call routing decisions. THE AUDIT LOG. Synchronous writes only (Tech Paper §9).
-- ----------------------------------------------------------------------------
CREATE TABLE nautgate.route_decisions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    ts TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    agent_id TEXT NOT NULL,
    inbound_format TEXT NOT NULL,                     -- 'openai_chat'|'openai_responses'|'anthropic'
    model_requested TEXT,
    prompt_excerpt TEXT,                              -- first 200 chars of last user message (BODY_CAPTURE policy)
    prompt_tokens INT,                                -- estimated at decision time
    classified_tier TEXT NOT NULL,                    -- 'SIMPLE'|'MEDIUM'|'COMPLEX'|'REASONING'|'UNCLASSIFIED' (Day 4 fills real values)
    classified_score NUMERIC(6, 4),
    classified_signals JSONB,
    classified_sensitivity TEXT,                      -- 'none'|'pii'|'secret'
    brain_hints JSONB,                                -- what sb-brain returned (NULL when not enabled)
    decision_provider TEXT NOT NULL,
    decision_model TEXT NOT NULL,
    decision_reason TEXT,
    fallback_chain JSONB,                             -- [["provider","model"], ...]
    confidence NUMERIC(4, 3)
);
CREATE INDEX ON nautgate.route_decisions (ts DESC);
CREATE INDEX ON nautgate.route_decisions (agent_id, ts DESC);
CREATE INDEX ON nautgate.route_decisions (decision_provider, ts DESC);

-- ----------------------------------------------------------------------------
-- Per-call outcomes. Joined to route_decisions by id. Durable-spool fallback (Day 4).
-- Includes streaming-capture columns from Tech Paper §11.3.
-- ----------------------------------------------------------------------------
CREATE TABLE nautgate.route_outcomes (
    decision_id UUID PRIMARY KEY REFERENCES nautgate.route_decisions(id) ON DELETE CASCADE,
    ts TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    status_code INT NOT NULL,
    duration_ms INT NOT NULL,
    first_byte_ms INT,                                -- TTFT for streaming
    prompt_tokens INT,                                -- actual (from upstream usage)
    completion_tokens INT,
    reasoning_tokens INT,                             -- for reasoning models
    cost_usd NUMERIC(12, 6),
    was_empty BOOL DEFAULT FALSE,                     -- the Tongyi failure mode (completion_tokens > 0 AND content == "")
    used_fallback BOOL DEFAULT FALSE,
    fallback_count INT DEFAULT 0,
    client_disconnected BOOL DEFAULT FALSE,
    was_truncated BOOL DEFAULT FALSE,                 -- accumulator hit the cap (Tech Paper §11.3)
    truncated_at_byte INT
);
CREATE INDEX ON nautgate.route_outcomes (ts DESC);

-- ----------------------------------------------------------------------------
-- Provider health, denormalized for fast brain queries.
-- ----------------------------------------------------------------------------
CREATE TABLE nautgate.provider_health (
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    hour_bucket TIMESTAMPTZ NOT NULL,                 -- truncated to hour
    total_calls INT DEFAULT 0,
    success_calls INT DEFAULT 0,
    empty_calls INT DEFAULT 0,
    total_latency_ms BIGINT DEFAULT 0,
    total_cost_usd NUMERIC(12, 6) DEFAULT 0,
    PRIMARY KEY (provider, model, hour_bucket)
);
CREATE INDEX ON nautgate.provider_health (hour_bucket DESC);

-- ----------------------------------------------------------------------------
-- Per-agent routing preferences (read by sb-brain for ladder levels 4 / 6).
-- ----------------------------------------------------------------------------
CREATE TABLE nautgate.routing_preferences (
    agent_id TEXT PRIMARY KEY,
    preferred_tier_overrides JSONB,                   -- {"code_review": "REASONING"}
    banned_models TEXT[],
    preferred_models TEXT[],
    notes TEXT,
    updated_at TIMESTAMPTZ DEFAULT NOW()
);
