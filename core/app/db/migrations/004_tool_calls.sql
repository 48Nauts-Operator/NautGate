-- NautGate schema v4: capture the model's actual tool_calls in route_outcomes.
-- We already have tools_count (how many tools the CLI shipped) on
-- route_decisions. This adds the matching "what did the model call back" view
-- so the audit log can show: tools_offered=5, tools_called=[search, edit].

ALTER TABLE nautgate.route_outcomes
    ADD COLUMN IF NOT EXISTS tool_calls_made JSONB;
