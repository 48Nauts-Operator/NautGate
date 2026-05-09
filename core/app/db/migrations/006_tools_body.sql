-- Capture the tools array sent upstream so the audit log can show
-- exactly what tool definitions the provider received.
ALTER TABLE nautgate.route_decisions
    ADD COLUMN IF NOT EXISTS tools_body TEXT,
    ADD COLUMN IF NOT EXISTS tools_body_truncated_at_byte INTEGER;
