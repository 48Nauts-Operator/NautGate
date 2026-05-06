# NautGate dev tasks. Run `just` to list.

default:
    @just --list

# --- Tests / lint ---

test:
    cd core && uv run pytest

lint:
    cd core && uv run ruff check .
    cd core && uv run ruff format --check .

fix:
    cd core && uv run ruff check --fix .
    cd core && uv run ruff format .

# --- Local dev ---

dev:
    cd core && uv run uvicorn app.main:app --reload --host 0.0.0.0 --port 8090

sync:
    cd core && uv sync

# --- Docker ---

up:
    docker compose -f deploy/docker-compose.yml up -d

up-db:
    docker compose -f deploy/docker-compose.yml up -d nautgate-db

down:
    docker compose -f deploy/docker-compose.yml down

logs:
    docker compose -f deploy/docker-compose.yml logs -f

# --- DB ---

psql:
    docker compose -f deploy/docker-compose.yml exec nautgate-db psql -U nautgate -d nautgate

# Issue a fresh API key for an agent. Prints the plaintext token ONCE.
# Usage: just issue-key alice
issue-key agent_id profile="auto":
    cd core && NAUTGATE_DB_URL="${NAUTGATE_DB_URL:-postgres://nautgate:nautgate@localhost:5432/nautgate}" uv run python ../scripts/issue_key.py --agent-id {{agent_id}} --profile {{profile}}

# --- Git ---

push:
    git push origin main

pull:
    git pull --rebase origin main
