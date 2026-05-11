-- Projects: a grouping layer above keys. Several keys (potentially from
-- different agents — Claude Code, Pi, Codex, OpenCode — working on the
-- same effort) can share a project_id and aggregate as one cost center.
--
-- project_id is intentionally a free-form TEXT label, not a FK to a separate
-- projects table. The label IS the identity; lifecycle is just "key exists
-- with this label, project exists." Cheap to add, easy to retire.

ALTER TABLE nautgate.api_keys
    ADD COLUMN IF NOT EXISTS project_id TEXT;

-- Denormalize onto route_decisions so cost queries can filter by project
-- without a join. Stamped at PRECAPTURE time from the api_keys row.
ALTER TABLE nautgate.route_decisions
    ADD COLUMN IF NOT EXISTS project_id TEXT;

CREATE INDEX IF NOT EXISTS api_keys_project_idx
    ON nautgate.api_keys (project_id) WHERE project_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS route_decisions_project_ts_idx
    ON nautgate.route_decisions (project_id, ts DESC) WHERE project_id IS NOT NULL;
