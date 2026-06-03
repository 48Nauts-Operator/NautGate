-- OAuth passthrough columns on route_outcomes.
--
-- When a Claude (or ChatGPT) request is served via the user's Max/Pro
-- subscription rather than a metered API key, ``cost_usd`` is 0.00 (we
-- didn't pay per-token). We still want to show what the call WOULD have
-- cost on metered billing so the dashboard can surface
-- "subscription savings = sum(notional_cost_usd) where cost_usd = 0".
--
-- ``rate_limited_429`` tags calls that hit the subscription's per-window
-- rate limit. Counts towards a separate dashboard metric so the operator
-- knows when their subscription is the bottleneck vs. when something else
-- went wrong.

ALTER TABLE nautgate.route_outcomes
    ADD COLUMN IF NOT EXISTS notional_cost_usd NUMERIC(12,6),
    ADD COLUMN IF NOT EXISTS rate_limited_429  BOOLEAN NOT NULL DEFAULT false;

CREATE INDEX IF NOT EXISTS route_outcomes_rate_limited_idx
    ON nautgate.route_outcomes (ts DESC)
    WHERE rate_limited_429 = true;
