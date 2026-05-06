# Quickstart

Get NautGate running locally in under 2 minutes.

## Prereqs

- macOS or Linux
- Docker + docker compose v2
- [uv](https://docs.astral.sh/uv/) (`brew install uv` or `curl -LsSf https://astral.sh/uv/install.sh | sh`)
- (optional) `just` (`brew install just`)

## 1. Start the database

```bash
docker compose -f deploy/docker-compose.yml up -d nautgate-db
```

Healthcheck takes ~5 seconds. Verify:

```bash
docker compose -f deploy/docker-compose.yml exec nautgate-db pg_isready -U nautgate
```

## 2. Install Python deps

```bash
cd core
uv sync                         # creates .venv, installs from pyproject.toml + lockfile
```

## 3. Run the gateway

```bash
NAUTGATE_DB_URL=postgres://nautgate:nautgate@localhost:5432/nautgate \
  uv run uvicorn app.main:app --host 0.0.0.0 --port 8090
```

On startup, the gateway:
- opens an asyncpg pool to `NAUTGATE_DB_URL`
- applies migrations (idempotent — checks if schema `nautgate` exists)
- mounts `/health`, `/ready`, `/v1/*` routes

## 4. Smoke test

```bash
# Always 200
curl -fsS http://localhost:8090/health

# 200 if DB reachable, else 503
curl -isS http://localhost:8090/ready

# Day 1 stub — returns 501 with the "coming in" marker header
curl -isS -X POST http://localhost:8090/v1/chat/completions \
  -H 'Content-Type: application/json' -d '{}'
# → HTTP/1.1 501 Not Implemented
# → x-nautgate-coming-in: week-1
```

## 5. Verify schema

```bash
docker compose -f deploy/docker-compose.yml exec nautgate-db \
  psql -U nautgate -d nautgate -c '\dt nautgate.*'
```

Expected: 5 tables — `api_keys`, `route_decisions`, `route_outcomes`, `provider_health`, `routing_preferences`.

## 6. Tests

```bash
cd core
uv run pytest
```

## Teardown

```bash
docker compose -f deploy/docker-compose.yml down            # keep volume
docker compose -f deploy/docker-compose.yml down -v         # nuke DB
```

## Day 2+

Day 2 adds the NautRouter sidecar. After cloning to `vendor/NautRouter`, real `/v1/chat/completions` will route through it — see [`architecture.md`](architecture.md) and the Build Plan.
