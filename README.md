# NautGate

> One gateway for every LLM call. Routes to the right model, captures every byte, learns from history, protects what's sensitive.

Memory-aware LLM gateway. Wraps [NautRouter](https://github.com/48Nauts-Operator/NautRouter) (TS, scoring engine + format translation) inside a Python service that adds capture, classification, plugin extensions, and the durability contract.

## Status

**Week 1, Day 1** scaffold. Repo layout, schema, 501 stubs, tests. Real `/v1/chat/completions` lands Day 2; streaming Day 3.

See the blueprint:

- `Knowledge/Obsidian/Business/NautCoder/05-Development/01-Blueprint/NautGate/00 — Index.md` (canonical entry point)
- Vision · Concept · Working Paper · Build Plan · Tools Inventory · Tech Paper · Risks & Rollback

## Quickstart

```bash
docker compose -f deploy/docker-compose.yml up -d nautgate-db
cd core && uv sync
NAUTGATE_DB_URL=postgres://nautgate:nautgate@localhost:5432/nautgate \
  uv run uvicorn app.main:app --host 0.0.0.0 --port 8090
```

```bash
curl http://localhost:8090/health                           # → 200
curl -isS -X POST http://localhost:8090/v1/chat/completions # → 501 with X-Nautgate-Coming-In header (Day 1 stub)
```

Full quickstart: [`docs/quickstart.md`](docs/quickstart.md). Architecture: [`docs/architecture.md`](docs/architecture.md).

## Layout

```
core/         FastAPI gateway (Python 3.12, uv)
extensions/   Sidecar microservices (sb-capture, sb-brain, sb-privacy) — Week 2+
deploy/       docker-compose stacks
config/       nautgate.yaml example
docs/         quickstart, architecture
scripts/      operational scripts
vendor/       wrapped NautRouter source (Day 2+)
memory/       repo-local Claude memory index
```

## Development

```bash
just test     # cd core && uv run pytest
just lint     # uv run ruff check .
just fix      # uv run ruff check --fix . && uv run ruff format .
just up       # docker compose -f deploy/docker-compose.yml up -d
just down     # docker compose -f deploy/docker-compose.yml down
```

## Repo

`48Nauts-Operator/NautGate`, private. Issues + PRs welcome from the 48Nauts org.
