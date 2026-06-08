-- Migration 016: anti_pattern column on quality_evals.
-- The judge now emits a short one-phrase description of WHAT the user did
-- wrong in their prompt (e.g. "Asked for 3 things in one prompt"). This
-- field powers the anti-pattern aggregate ("what NOT to say to your LLM")
-- on the Quality tab.

ALTER TABLE nautgate.quality_evals
    ADD COLUMN IF NOT EXISTS anti_pattern TEXT;

CREATE INDEX IF NOT EXISTS quality_evals_anti_pattern_idx
    ON nautgate.quality_evals (anti_pattern)
 WHERE anti_pattern IS NOT NULL;
