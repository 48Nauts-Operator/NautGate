-- Single-row config table for runtime-tunable settings that operators
-- might want to flip from the Dashboard without editing .env and
-- restarting the gateway. Today: SecondBrain memory ingest toggle +
-- connection. Tomorrow: anything else we add a UI for.
--
-- Why one jsonb blob instead of typed columns: lets us add new knobs
-- without a migration per knob. Defaults are sensible so an empty row
-- still works.

CREATE TABLE IF NOT EXISTS nautgate.app_config (
    id          INT         PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    settings    JSONB       NOT NULL DEFAULT '{}'::jsonb,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

INSERT INTO nautgate.app_config (id, settings)
VALUES (1, jsonb_build_object(
    'sb_ingest', jsonb_build_object(
        'enabled', false,
        'host', '100.71.163.122',
        'port', 5433,
        'database', 'agents_memory',
        'user', 'agents'
        -- password intentionally not stored here; read from env at runtime
    )
))
ON CONFLICT (id) DO NOTHING;
