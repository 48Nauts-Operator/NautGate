# CLAUDE.md — NautGate repo guide

NautGate is a memory-aware LLM gateway: Python (FastAPI + asyncpg + httpx) wrapping NautRouter (TS sidecar) for routing + format translation, with optional sidecar extensions for capture, brain, and privacy.

## Canonical blueprint

`/Users/cand0rian/Knowledge/Obsidian/Business/NautCoder/05-Development/01-Blueprint/NautGate/`
- `00 — Index.md` — entry point, decision log
- `01 — Vision.md`
- `02 — Concept.md` — architecture + plugin contract
- `04 — Build Plan.md` — week-by-week scope and deliverables
- `06 — Tech Paper.md` — implementation detail (DDL §10, streaming §11, indexes §12, brain ladder §2.5, performance budgets §7)
- `07 — Risks & Rollback.md`

When in doubt, the blueprint wins over what's in the code. Update the blueprint OR add a memory note explaining why the code drifted.

## Current scope

**Week 1: OpenAI Chat only** (`/v1/chat/completions`). Anthropic Messages and OpenAI Responses inbound formats are Week 1b. Sidecars are Week 2+.

**Day status banner is in `README.md`.**

## Stack

- Python 3.12 + FastAPI + uvicorn[standard]
- asyncpg + Postgres 16 (own schema `nautgate` in `agents_postgres`)
- httpx (async, http/1.1 keepalive — NautRouter doesn't speak h2)
- pydantic + pydantic-settings + structlog + pyyaml
- pytest + pytest-asyncio + ruff
- uv for dependency management (`core/pyproject.toml`, `core/uv.lock` committed)

## Commands

```bash
just test          # uv run pytest
just lint          # ruff check
just fix           # ruff check --fix && ruff format
just up / down     # docker compose -f deploy/docker-compose.yml ...
just dev           # uvicorn app.main:app --reload
```

## Conventions

- Endpoints not yet implemented return `501 Not Implemented` with header `X-Nautgate-Coming-In: <day-N or week-Nx>`. Never 404 on a path that's coming.
- `route_decisions` writes are **synchronous** (audit log). `route_outcomes` and extension calls are fire-and-forget. Per Tech Paper §9.
- Stream capture uses the **tee pattern** with an 8 MB cap, truncated at SSE event boundaries. Per Tech Paper §11.
- Sensitive content classification gates body capture, not metadata capture. Per Concept §"Capture order".
- Three-stage capture: PRECAPTURE (metadata) → CLASSIFY → BODY_CAPTURE (policy-gated).

## Memory

`memory/MEMORY.md` is the auto-memory index for repo-local Claude sessions. Add memories there as the codebase teaches us things the blueprint hasn't already documented.
