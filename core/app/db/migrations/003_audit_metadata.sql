-- NautGate schema v3: per-call audit metadata (Audit Log view).
-- Mirrors the columns SB's proxy live-feed surfaces: source identity,
-- payload structure, byte sizes.

ALTER TABLE nautgate.route_decisions
    ADD COLUMN IF NOT EXISTS source_ip          INET,
    ADD COLUMN IF NOT EXISTS source_hostname    TEXT,
    ADD COLUMN IF NOT EXISTS messages_count     INT,
    ADD COLUMN IF NOT EXISTS tools_count        INT,
    ADD COLUMN IF NOT EXISTS stream_flag        BOOLEAN,
    ADD COLUMN IF NOT EXISTS request_size_bytes INT;

ALTER TABLE nautgate.route_outcomes
    ADD COLUMN IF NOT EXISTS response_size_bytes INT;
