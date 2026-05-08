# sb-brain

NautGate extension. Subscribes to `before_route` (synchronous, 50ms budget)
and `on_outcome` (cache invalidation). Reads NautGate's own
`provider_health` and `routing_preferences` tables.

## What it does (v1)

- **Provider-health demotions** (last 6h): if `(provider, model)` has empty
  rate ≥ 30%, mark it `demoted_models`.
- **Agent preferences**: mirror `routing_preferences.banned_models` /
  `preferred_models` so they apply to `auto` routing.
- **Tier nudge**: if the agent's last 7 days skewed ≥60% to one tier and
  the current request scored to a different tier, suggest the agent's
  modal tier as `preferred_tier`. NautGate honors this only when score
  confidence is low (per Tech Paper §2.5 level 7).

## Configuration

```
SB_BRAIN_DB_URL       postgres://nautgate:nautgate@nautgate-db:5432/nautgate
SB_BRAIN_TIMEOUT_MS   50
SB_BRAIN_LOG_LEVEL    INFO
```

## Wire into NautGate

`nautgate.yaml`:

```yaml
extensions:
  sb-brain:
    base_url: http://sb-brain:8002
    hooks: [before_route, on_outcome]
    timeout_ms_before_route: 50
    timeout_ms: 200
```

## Run standalone

```bash
cd extensions/sb-brain
uv sync
SB_BRAIN_DB_URL=postgres://nautgate:nautgate@localhost:5432/nautgate uv run uvicorn main:app --port 8002
```

Index migrations run idempotently on startup (per Tech Paper §12.1).

## Cache behavior (Tech Paper §12.3)

- LRU + TTL, per agent_id, capacity 1000, TTL 5 min.
- `on_outcome` for an agent invalidates that agent's entry.
- `tier_nudge` is recomputed against the current request's tier on every
  call even on cache hit, since it depends on what the score said.

## Degrade gracefully (Tech Paper §12.4)

If the DB roundtrip exceeds `SB_BRAIN_TIMEOUT_MS`, the endpoint returns
`{}`. NautGate falls through to its score-based decision; the request is
never blocked by sb-brain.
