-- Prompt-diet trials: shadow testing on the PROMPT axis. Same model called
-- twice — original prompt (champion) vs pruned prompt (challenger) — and the
-- blind judge compares the answers. Reuses shadow_trials; these columns
-- distinguish the trial type and record how much payload the diet removed.

ALTER TABLE nautgate.shadow_trials
    ADD COLUMN IF NOT EXISTS trial_type     TEXT NOT NULL DEFAULT 'model',  -- 'model' | 'prompt_diet'
    ADD COLUMN IF NOT EXISTS diet_strategy  TEXT,                            -- e.g. 'history-6'
    ADD COLUMN IF NOT EXISTS original_bytes INTEGER,
    ADD COLUMN IF NOT EXISTS pruned_bytes   INTEGER;

CREATE INDEX IF NOT EXISTS shadow_trials_type_idx ON nautgate.shadow_trials (trial_type);

-- Diet defaults into the shadow config section. diet_apply maps agent_id →
-- strategy for agents where a proven diet is applied in-flight.
UPDATE nautgate.app_config
   SET settings = jsonb_set(settings, '{shadow}',
        (settings->'shadow')
        || jsonb_build_object('diet_enabled', false,
                              'diet_strategy', 'history-6',
                              'diet_apply', '{}'::jsonb))
 WHERE id = 1
   AND settings ? 'shadow'
   AND NOT (settings->'shadow' ? 'diet_enabled');
