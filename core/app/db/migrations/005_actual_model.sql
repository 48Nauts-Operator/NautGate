-- NautGate schema v5: capture the model the upstream provider actually used.
--
-- We already store decision_model (NautGate's pick — e.g. "openrouter/auto")
-- but when OpenRouter or any meta-provider does its own selection on top,
-- the real model used is only visible in the upstream response body's
-- `model` field. Surface it as a first-class column so the audit log
-- can show "decision → actual" without parsing the body every time.

ALTER TABLE nautgate.route_outcomes
    ADD COLUMN IF NOT EXISTS actual_model    TEXT,
    ADD COLUMN IF NOT EXISTS actual_provider TEXT;
