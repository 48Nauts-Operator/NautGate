-- Prompt-cache accounting on route_outcomes.
--
-- Providers expose cache usage in their response ``usage`` object, but until
-- now NautGate dropped it: cost was computed off input/output only, so the
-- cache_read/cache_write tiers in pricing.yaml were dead config and the
-- "subscription savings" number on the Cost page was incomplete.
--
-- Three new columns, all populated by app.usage.normalize_usage():
--   cache_read_tokens   tokens served from the provider's cache (cheap tier)
--   cache_write_tokens  tokens written to cache (Anthropic's 25% premium tier;
--                       ~always 0/NULL for OpenAI/DeepSeek/Gemini — no write premium)
--   prefix_hash         sha1 of the cacheable request prefix (system + tools),
--                       for the cache-reuse / silent-cache-break leak detector
--
-- SEMANTICS CHANGE (new rows only): as of this migration, ``prompt_tokens`` is
-- the FRESH (non-cached) input count, consistent across providers, so that
--   total_input = prompt_tokens + cache_read_tokens + cache_write_tokens
-- Previously OpenAI-shaped rows stored the TOTAL prompt count (cache reads
-- included). This is a correction; historical rows are not backfilled (the
-- split is unrecoverable). Mixed old/new rows: treat pre-018 prompt_tokens as
-- "total-ish" and post-018 as "fresh".

ALTER TABLE nautgate.route_outcomes
    ADD COLUMN IF NOT EXISTS cache_read_tokens  INT,
    ADD COLUMN IF NOT EXISTS cache_write_tokens INT,
    ADD COLUMN IF NOT EXISTS prefix_hash        TEXT;

-- Leak detector groups by prefix_hash; partial index skips the NULL majority
-- (calls with no cacheable prefix).
CREATE INDEX IF NOT EXISTS route_outcomes_prefix_hash_idx
    ON nautgate.route_outcomes (prefix_hash)
    WHERE prefix_hash IS NOT NULL;
