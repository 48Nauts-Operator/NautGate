#!/usr/bin/env bash
# NautGate process manager. Start, stop, restart, status, tail logs.
#
# Usage:
#   scripts/nautgate.sh start      # ensure DB is up + launch uvicorn (bg) + open dashboard
#   scripts/nautgate.sh stop       # kill uvicorn (leaves DB running)
#   scripts/nautgate.sh restart    # stop + start
#   scripts/nautgate.sh status     # show what's running and how to reach it
#   scripts/nautgate.sh logs       # tail -f the uvicorn log
#   scripts/nautgate.sh open       # just open the dashboard (no restart)
#   scripts/nautgate.sh down       # full stop (uvicorn + DB)
#
# Reads core/.env (and repo-root .env) automatically via python-dotenv on
# uvicorn boot. No need to source anything beforehand.
#
# Env flags:
#   NAUTGATE_PORT=8090            override listen port
#   NAUTGATE_HOST=0.0.0.0         override listen interface
#   NAUTGATE_RELOAD=1             add --reload for hot-reload dev (off by default)
#   NAUTGATE_BROWSER=Safari       open dashboard in a specific app
#                                 (or "cmux" for cmux's webview, or "none" to skip)
#   NO_BROWSER=1                  skip auto-open (alias for NAUTGATE_BROWSER=none)

set -euo pipefail

# --- Paths --------------------------------------------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
CORE_DIR="$REPO_ROOT/core"
LOG_DIR="$REPO_ROOT/logs"
LOG_FILE="$LOG_DIR/nautgate.log"
PID_FILE="$LOG_DIR/nautgate.pid"
COMPOSE_FILE="$REPO_ROOT/deploy/docker-compose.yml"

HOST="${NAUTGATE_HOST:-0.0.0.0}"
PORT="${NAUTGATE_PORT:-8090}"
RELOAD_FLAG=""
if [[ "${NAUTGATE_RELOAD:-0}" == "1" ]]; then
    RELOAD_FLAG="--reload"
fi

# Default the DB URL to the local docker-compose Postgres. This matches what
# Load API keys + sidecar config from deploy/.env so things like the
# quality_eval judge and the behavioral canary suite can reach OpenRouter
# without the operator having to also export the keys in their shell.
# `set -a` exports any vars assigned by the sourced file; `set +a` restores
# normal scoping immediately after. Silent no-op if the file isn't there.
_NG_DEPLOY_ENV="$(cd "$(dirname "$0")/.." && pwd)/deploy/.env"
if [[ -f "$_NG_DEPLOY_ENV" ]]; then
    set -a
    # shellcheck disable=SC1090
    source "$_NG_DEPLOY_ENV"
    set +a
fi

# deploy/docker-compose.yml exposes on 127.0.0.1:5432 (user/pass: nautgate,
# db: nautgate). Override via env or core/.env if you point at a different DB.
export NAUTGATE_DB_URL="${NAUTGATE_DB_URL:-postgres://nautgate:nautgate@127.0.0.1:5432/nautgate}"
# Sensible default sidecar URL too — same as the Compose service binding.
export NAUTROUTER_BASE_URL="${NAUTROUTER_BASE_URL:-http://localhost:8404}"

# Browser auto-open after a successful start.
#   NAUTGATE_BROWSER=Safari        → open -a "Safari" <url>
#   NAUTGATE_BROWSER=cmux          → open in cmux's webview (uses `cmux open`)
#   NAUTGATE_BROWSER=none          → never open
#   (unset)                        → use macOS default browser via `open`
OPEN_BROWSER=1
if [[ "${NAUTGATE_BROWSER:-}" == "none" ]] || [[ "${NO_BROWSER:-0}" == "1" ]]; then
    OPEN_BROWSER=0
fi

# --- Colour helpers (only when stdout is a tty) -------------------------
if [[ -t 1 ]]; then
    C_RESET=$'\033[0m'; C_GREEN=$'\033[32m'; C_RED=$'\033[31m'
    C_YEL=$'\033[33m'; C_DIM=$'\033[2m'
else
    C_RESET=''; C_GREEN=''; C_RED=''; C_YEL=''; C_DIM=''
fi

info()  { echo "${C_DIM}→${C_RESET} $*"; }
ok()    { echo "${C_GREEN}✓${C_RESET} $*"; }
warn()  { echo "${C_YEL}!${C_RESET} $*"; }
err()   { echo "${C_RED}✗${C_RESET} $*" >&2; }

# --- DB up? -------------------------------------------------------------
ensure_db() {
    if docker ps --format '{{.Names}}' | grep -q '^nautgate-db$'; then
        ok "nautgate-db is already running"
        return 0
    fi
    info "starting nautgate-db via docker compose…"
    docker compose -f "$COMPOSE_FILE" up -d nautgate-db >/dev/null
    # Wait for healthy (max ~20s)
    for _ in {1..20}; do
        if docker inspect --format '{{.State.Health.Status}}' nautgate-db 2>/dev/null | grep -q healthy; then
            ok "nautgate-db is healthy"
            return 0
        fi
        sleep 1
    done
    warn "nautgate-db started but didn't report healthy in 20s — continuing anyway"
}

# --- uvicorn PID lookup -------------------------------------------------
# Prefer the PID file when it exists and is alive; otherwise scan ps for our
# specific uvicorn invocation so a stale PID file doesn't leave a zombie behind.
find_uvicorn_pid() {
    if [[ -f "$PID_FILE" ]]; then
        local saved
        saved="$(cat "$PID_FILE" 2>/dev/null || true)"
        if [[ -n "$saved" ]] && kill -0 "$saved" 2>/dev/null; then
            echo "$saved"
            return 0
        fi
    fi
    # Fall back to ps. Match our exact bind to avoid clobbering another
    # uvicorn project that happens to share the host.
    pgrep -f "uvicorn app\.main:app .*--port $PORT" 2>/dev/null | head -n 1 || true
}

# --- Commands -----------------------------------------------------------
cmd_start() {
    local existing
    existing="$(find_uvicorn_pid)"
    if [[ -n "$existing" ]]; then
        warn "uvicorn already running (pid $existing) — use restart to bounce"
        return 0
    fi
    ensure_db
    mkdir -p "$LOG_DIR"

    info "launching uvicorn on $HOST:$PORT $RELOAD_FLAG"
    cd "$CORE_DIR"
    # nohup keeps it alive past shell exit; disown detaches it from the
    # shell's job table. macOS doesn't ship setsid, so we don't rely on a
    # dedicated process group — stop() walks pgrep -P to find the uvicorn
    # child that `uv run` spawns.
    nohup uv run uvicorn app.main:app \
        --host "$HOST" --port "$PORT" $RELOAD_FLAG \
        >>"$LOG_FILE" 2>&1 < /dev/null &
    local pid=$!
    disown "$pid" 2>/dev/null || true
    echo "$pid" > "$PID_FILE"
    # Brief health check so the operator gets immediate feedback if it crashed.
    sleep 2
    if ! kill -0 "$pid" 2>/dev/null; then
        err "uvicorn exited within 2s — tailing the log:"
        tail -n 30 "$LOG_FILE" || true
        return 1
    fi
    # Probe /health — give it up to ~6s to come up before opening a browser.
    local ready=0
    for _ in {1..6}; do
        if command -v curl >/dev/null && \
           curl -fsS --max-time 1 "http://127.0.0.1:$PORT/health" >/dev/null 2>&1; then
            ready=1
            break
        fi
        sleep 1
    done
    local url="http://localhost:$PORT/dashboard"
    if [[ $ready -eq 1 ]]; then
        ok "uvicorn up (pid $pid) — $url"
        # /ready returns 503 when the DB pool isn't healthy — warn early
        # rather than letting the user discover via dashboard 503s.
        if ! curl -fsS --max-time 2 "http://127.0.0.1:$PORT/ready" >/dev/null 2>&1; then
            warn "/ready failed — the DB pool isn't healthy. Check logs:"
            grep -E "no_db_url_configured|db_pool_failed" "$LOG_FILE" | tail -3 || true
            info "DB URL in use: ${NAUTGATE_DB_URL%%@*}@…"
        fi
        if [[ $OPEN_BROWSER -eq 1 ]]; then
            open_dashboard "$url"
        fi
    else
        warn "uvicorn started (pid $pid) but /health didn't respond yet — give it a few seconds"
        info "tail logs: scripts/nautgate.sh logs"
    fi
}

# Open the dashboard. Prefers an explicit NAUTGATE_BROWSER, then macOS
# `open`, then xdg-open. Silent on failure — opening a browser is a nice-to-
# have, not a reason to fail the start.
open_dashboard() {
    local url="$1"
    local browser="${NAUTGATE_BROWSER:-}"
    case "$browser" in
        cmux)
            # cmux ships a CLI; try a few invocation shapes since the API
            # surface evolves. All fall through silently to the default.
            if command -v cmux >/dev/null 2>&1; then
                cmux open "$url"            2>/dev/null && return 0
                cmux browser open "$url"    2>/dev/null && return 0
                cmux webview "$url"         2>/dev/null && return 0
            fi
            ;;
        "")
            : ;;
        none)
            return 0 ;;
        *)
            # Treat as a macOS .app name (Safari, Google Chrome, Firefox,
            # "Real Browser", etc.) — `open -a` resolves it.
            if command -v open >/dev/null 2>&1; then
                open -a "$browser" "$url" 2>/dev/null && return 0
            fi
            ;;
    esac
    # Defaults
    if command -v open >/dev/null 2>&1; then
        open "$url" 2>/dev/null && return 0
    fi
    if command -v xdg-open >/dev/null 2>&1; then
        xdg-open "$url" >/dev/null 2>&1 && return 0
    fi
    info "couldn't auto-open browser — point one at $url"
}

cmd_stop() {
    local parent
    parent="$(find_uvicorn_pid)"
    if [[ -z "$parent" ]]; then
        info "no uvicorn process running"
        rm -f "$PID_FILE"
        return 0
    fi
    info "stopping uvicorn (pid $parent)…"
    # Collect the `uv run` parent + any uvicorn children it spawned.
    # macOS pgrep doesn't recurse, so do it ourselves one level deep.
    local pids=("$parent")
    local children
    children="$(pgrep -P "$parent" 2>/dev/null || true)"
    if [[ -n "$children" ]]; then
        while read -r c; do [[ -n "$c" ]] && pids+=("$c"); done <<<"$children"
        # Also one more level — the actual uvicorn worker is a grandchild.
        for c in $children; do
            local gc
            gc="$(pgrep -P "$c" 2>/dev/null || true)"
            [[ -n "$gc" ]] && while read -r g; do [[ -n "$g" ]] && pids+=("$g"); done <<<"$gc"
        done
    fi
    # TERM each
    for p in "${pids[@]}"; do
        kill -TERM "$p" 2>/dev/null || true
    done
    # Wait up to ~5s for the parent to exit
    for _ in {1..15}; do
        if ! kill -0 "$parent" 2>/dev/null; then break; fi
        sleep 0.3
    done
    # Escalate any survivors
    for p in "${pids[@]}"; do
        if kill -0 "$p" 2>/dev/null; then
            warn "pid $p didn't stop on TERM — sending KILL"
            kill -KILL "$p" 2>/dev/null || true
        fi
    done
    rm -f "$PID_FILE"
    ok "stopped"
}

cmd_restart() {
    cmd_stop
    cmd_start
}

cmd_status() {
    local pid
    pid="$(find_uvicorn_pid)"
    if [[ -n "$pid" ]]; then
        ok "uvicorn running (pid $pid)"
        if command -v curl >/dev/null && curl -fsS --max-time 2 "http://127.0.0.1:$PORT/health" >/dev/null 2>&1; then
            echo "  ${C_DIM}health:${C_RESET}   http://localhost:$PORT/health  ${C_GREEN}OK${C_RESET}"
        else
            echo "  ${C_DIM}health:${C_RESET}   http://localhost:$PORT/health  ${C_RED}unreachable${C_RESET}"
        fi
        echo "  ${C_DIM}dashboard:${C_RESET} http://localhost:$PORT/dashboard"
        echo "  ${C_DIM}log:${C_RESET}       $LOG_FILE"
    else
        warn "uvicorn not running"
    fi

    if docker ps --format '{{.Names}}' | grep -q '^nautgate-db$'; then
        local health
        health="$(docker inspect --format '{{.State.Health.Status}}' nautgate-db 2>/dev/null || echo unknown)"
        ok "nautgate-db running  (health: $health)"
    else
        warn "nautgate-db not running — start with: scripts/nautgate.sh start"
    fi
}

cmd_logs() {
    mkdir -p "$LOG_DIR"
    touch "$LOG_FILE"
    info "tailing $LOG_FILE — Ctrl-C to stop"
    tail -n 50 -f "$LOG_FILE"
}

cmd_down() {
    cmd_stop
    info "stopping nautgate-db…"
    docker compose -f "$COMPOSE_FILE" stop nautgate-db >/dev/null 2>&1 || true
    ok "all down"
}

# --- Dispatch -----------------------------------------------------------
cmd_open() {
    open_dashboard "http://localhost:$PORT/dashboard"
}

case "${1:-}" in
    start)   cmd_start ;;
    stop)    cmd_stop ;;
    restart) cmd_restart ;;
    status|"") cmd_status ;;
    logs)    cmd_logs ;;
    open)    cmd_open ;;
    down)    cmd_down ;;
    -h|--help)
        sed -n '2,22p' "$0"
        ;;
    *)
        err "unknown subcommand: $1"
        sed -n '2,22p' "$0"
        exit 2
        ;;
esac
