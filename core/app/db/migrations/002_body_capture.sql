-- NautGate schema v2: body capture columns (Day 4c).
-- prompt_body / response_body are policy-gated by classified_sensitivity:
--   none   → full text captured
--   pii    → captured with PII spans redacted
--   secret → NULL (metadata only)
-- Per Concept §"Capture order".

ALTER TABLE nautgate.route_decisions
    ADD COLUMN IF NOT EXISTS prompt_body TEXT,
    ADD COLUMN IF NOT EXISTS prompt_body_truncated_at_byte INT;

ALTER TABLE nautgate.route_outcomes
    ADD COLUMN IF NOT EXISTS response_body TEXT,
    ADD COLUMN IF NOT EXISTS response_body_truncated_at_byte INT;
