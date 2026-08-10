-- Backups shipped as: every 3 hours, keep 20. On a real instance a dump is
-- several gigabytes, so that default alone reaches ~166 GB at steady state, and
-- every fresh database inherited it.
--
-- Only rows still sitting on the exact old defaults are moved. Anyone who chose
-- their own schedule keeps it — an operator's deliberate setting is not ours to
-- overwrite.
UPDATE nautgate.backup_config
   SET interval_hours = 24,
       retention_count = 3,
       updated_at = now()
 WHERE id = 1
   AND interval_hours = 3
   AND retention_count = 20;
