-- Track 529/Overloaded retries absorbed by the OAuth forwarder.
--
-- When NautGate transparently retries an upstream 529 and the retry succeeds,
-- the outcome row's status_code is 200 — which would hide the fact that the
-- provider was shedding load. This column records how many 529s were absorbed
-- before success, so the provider-status widget can show the TRUE overload rate
-- even when the client never saw an error.

ALTER TABLE nautgate.route_outcomes
    ADD COLUMN IF NOT EXISTS upstream_overload_retries INT NOT NULL DEFAULT 0;
