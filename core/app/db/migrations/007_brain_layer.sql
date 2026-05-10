-- Brain layer (Tech Paper §2.5) — heuristic model scoring with audit trail.
--
-- Three concepts:
--   bloat findings:   per-request signals that data is being over-shipped
--   model_scorecard:  rolling per-(model, tier) score, time-decayed
--   model_incidents:  audit trail linking each de-rating event to its decision
--
-- Score is in [0, 1]. Starts neutral at 0.50. Each finding subtracts; clean
-- requests slowly add back. Routing skips a model when its score < 0.30.

-- 1) Per-decision bloat data (jsonb) + roll-up score + estimated waste in USD.
ALTER TABLE nautgate.route_decisions
    ADD COLUMN IF NOT EXISTS bloat_findings        JSONB,
    ADD COLUMN IF NOT EXISTS bloat_score           NUMERIC(4, 3),
    ADD COLUMN IF NOT EXISTS estimated_waste_usd   NUMERIC(10, 6);

CREATE INDEX IF NOT EXISTS route_decisions_bloat_score_idx
    ON nautgate.route_decisions (bloat_score DESC NULLS LAST)
    WHERE bloat_score IS NOT NULL;

-- 2) Per-(model, tier) scorecard. The brain's working memory.
CREATE TABLE IF NOT EXISTS nautgate.model_scorecard (
    provider          TEXT        NOT NULL,
    model             TEXT        NOT NULL,
    tier              TEXT        NOT NULL,
    score             NUMERIC(4, 3) NOT NULL DEFAULT 0.500,
    sample_size       INTEGER     NOT NULL DEFAULT 0,
    total_waste_usd   NUMERIC(12, 6) NOT NULL DEFAULT 0,
    last_updated      TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (provider, model, tier)
);

CREATE INDEX IF NOT EXISTS model_scorecard_score_idx
    ON nautgate.model_scorecard (tier, score ASC);

-- 3) Append-only incident log. Every de-rating event has evidence here.
CREATE TABLE IF NOT EXISTS nautgate.model_incidents (
    id                  UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    provider            TEXT        NOT NULL,
    model               TEXT        NOT NULL,
    tier                TEXT        NOT NULL,
    decision_id         UUID        NOT NULL REFERENCES nautgate.route_decisions(id) ON DELETE CASCADE,
    finding_type        TEXT        NOT NULL,  -- excessive_context | history_dominance | unused_capabilities | oversized_for_tier
    severity            TEXT        NOT NULL,  -- info | warn | crit
    score_penalty       NUMERIC(4, 3) NOT NULL,
    estimated_waste_usd NUMERIC(10, 6),
    ts                  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS model_incidents_model_ts_idx
    ON nautgate.model_incidents (provider, model, tier, ts DESC);

CREATE INDEX IF NOT EXISTS model_incidents_decision_idx
    ON nautgate.model_incidents (decision_id);
