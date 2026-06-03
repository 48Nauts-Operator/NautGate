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

# --- Process control (background uvicorn + DB) ---
# Thin wrappers over scripts/nautgate.sh — see that file for full docs.

start:
    scripts/nautgate.sh start

stop:
    scripts/nautgate.sh stop

restart:
    scripts/nautgate.sh restart

status:
    scripts/nautgate.sh status

tail:
    scripts/nautgate.sh logs

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

# Issue a fresh API key for an agent. Tees a copy to /tmp/ng-token-<agent>.txt
# so scrollback can't eat the token. Prints the ng_ line by itself at the end
# for easy copy-paste.
# Usage: just issue-key alice                       (profile=auto, no project)
#        just issue-key claude-code premium         (premium profile)
#        just issue-key claude-code auto nautgate   (group under project=nautgate)
issue-key agent_id profile="auto" project="":
    @echo "→ token will also be saved at /tmp/ng-token-{{agent_id}}.txt"
    cd core && NAUTGATE_DB_URL="${NAUTGATE_DB_URL:-postgres://nautgate:nautgate@localhost:5432/nautgate}" uv run python ../scripts/issue_key.py --agent-id {{agent_id}} --profile {{profile}} {{ if project != "" { "--project " + project } else { "" } }} 2>&1 | tee /tmp/ng-token-{{agent_id}}.txt
    @echo ""
    @echo "Token (copy this line):"
    @grep -E '^ng_' /tmp/ng-token-{{agent_id}}.txt || echo "(no ng_ line found — check output above)"

# List existing keys: id, agent_id, default_profile, created_at, last_used_at.
list-keys:
    docker compose -f deploy/docker-compose.yml exec nautgate-db psql -U nautgate -d nautgate -c "SELECT id, agent_id, default_profile, created_at, last_used_at FROM nautgate.api_keys ORDER BY created_at DESC;"

# Revoke a key by its id (uuid). The plaintext token stops authenticating.
# Find the id you want via `just list-keys`.
# Usage: just revoke-key 33eaa311-eebf-40fa-a220-7d72232ff1b3
revoke-key key_id:
    docker compose -f deploy/docker-compose.yml exec nautgate-db psql -U nautgate -d nautgate -c "DELETE FROM nautgate.api_keys WHERE id = '{{key_id}}'::uuid;"

# --- Git ---

push:
    git push origin main

pull:
    git pull --rebase origin main
