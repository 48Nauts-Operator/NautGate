# Architecture

One-page summary. The canonical detail is in the Obsidian blueprint.

## Shape

```
client ──HTTP──▶ NautGate (Python, FastAPI) ──HTTP keepalive──▶ NautRouter (TS, sidecar) ──HTTPS──▶ Provider
                       │
                       ├── PRECAPTURE (synchronous)         → route_decisions
                       ├── CLASSIFY  (regex + LLM-confirm)
                       ├── SCORE     (14 dims, NautRouter's algorithm)
                       ├── CONSULT BRAIN (optional, 50 ms)  → sb-brain (sidecar)
                       ├── DECIDE    (precedence ladder)
                       ├── FORWARD   (via NautRouter)
                       ├── RECORD    (tee accumulator, 8 MB cap)  → route_outcomes
                       └── extension hooks: on_request / on_response / after_route / on_outcome
```

## Why a wrapper, not a port

NautRouter (TS, ~943 LoC) already does scoring, format translation, SSE streaming, fallback chains. Wrapping is faster than porting and avoids re-deriving the streaming format translation. We can port later if the TS↔Python boundary causes pain. See Tech Paper §1.1.

## Two paths

- **Fast path** (≥99% of calls): regex sensitivity scan, score, decide, forward. ~30 ms p50 NautGate-overhead.
- **Privacy path**: when sensitivity classifier flags PII/secrets, divert to `sb-privacy` (anonymize → chunk → multi-provider dispatch → attest → assemble). Seconds-level. See Concept §"Privacy path".

## Plugin contract

5 hooks, all HTTP, all optional. Configured in `nautgate.yaml`:

| Hook | Sync? | Timeout | Purpose |
|---|---|---|---|
| `before_route` | sync | 50 ms hard | Brain hints (preferred tier, demoted models, override) |
| `on_request` | fire-forget | — | Mirror inbound to capture |
| `after_route` | fire-forget | — | Log decision |
| `on_response` | fire-forget | — | Mirror outbound (full assembled body for streams) |
| `on_outcome` | fire-forget | — | Latency, tokens, cost, was_empty |

Failures degrade silently: a buggy extension can never break a routing decision. See Tech Paper §6.

## Durability contract

| Table | Mode |
|---|---|
| `route_decisions` (audit log) | Synchronous — written before upstream forward returns. Process crash never loses an audit row. |
| `route_outcomes` | Durable spool — local SQLite WAL fallback if Postgres down, replay on recovery. |
| Extension hooks | Fire-and-forget. Drops counted; degraded state surfaced via `X-Nautgate-Ledger-State` header. |

See Tech Paper §9.

## Schema

Five tables in schema `nautgate`:

- `api_keys` — argon2id-hashed bearer tokens, agent_id, profile, budget, enabled providers, accumulator cap override
- `route_decisions` — per-call audit row written synchronously (the audit log)
- `route_outcomes` — per-call metrics joined by decision_id (tokens, cost, was_empty, was_truncated, client_disconnected)
- `provider_health` — denormalized hourly buckets for fast brain queries
- `routing_preferences` — per-agent preferred/banned models, tier overrides

Full DDL in Tech Paper §10. Migrations in `core/app/db/migrations/`.

## Pointers to the blueprint

- `00 — Index.md` — every doc, every decision
- `02 — Concept.md` — diagrams, plugin contract, paths
- `06 — Tech Paper.md` — DDL, scoring, streaming, indexes, brain ladder
- `04 — Build Plan.md` — week-by-week scope
- `07 — Risks & Rollback.md` — risks, rollback playbooks
