-- Backup metadata. Files live on disk under NAUTGATE_BACKUP_DIR (default
-- ~/.nautgate/backups/) — the DB just tracks what exists, when it was
-- made, and how big.

CREATE TABLE IF NOT EXISTS nautgate.backups (
    id              UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    ts              TIMESTAMPTZ NOT NULL DEFAULT now(),
    file_path       TEXT        NOT NULL,
    size_bytes      BIGINT      NOT NULL DEFAULT 0,
    created_via     TEXT        NOT NULL,  -- 'manual' | 'scheduled'
    table_counts    JSONB,                 -- {"route_decisions": 1234, ...}
    notes           TEXT,
    status          TEXT        NOT NULL DEFAULT 'ok',  -- 'ok' | 'failed' | 'in_progress'
    error_message   TEXT
);

CREATE INDEX IF NOT EXISTS backups_ts_idx ON nautgate.backups (ts DESC);

-- Single-row config table. Created with sane defaults on first migration.
CREATE TABLE IF NOT EXISTS nautgate.backup_config (
    id              INT         PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    enabled         BOOLEAN     NOT NULL DEFAULT TRUE,
    interval_hours  INT         NOT NULL DEFAULT 3,
    retention_count INT         NOT NULL DEFAULT 20,
    last_run_at     TIMESTAMPTZ,
    next_run_at     TIMESTAMPTZ,
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

INSERT INTO nautgate.backup_config (id) VALUES (1)
    ON CONFLICT (id) DO NOTHING;
