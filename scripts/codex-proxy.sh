#!/usr/bin/env bash
# Codex capture proxy. A mitmproxy forward-proxy that tees Codex's ChatGPT-OAuth
# `responses` traffic into NautGate's /v1/ingest endpoint (current Codex ignores
# OPENAI_BASE_URL, so the old codexps base-URL trick no longer works — it honours
# HTTPS_PROXY + a trusted CA instead).
#
# The addon POSTs to NautGate over HTTP; set NAUTGATE_INGEST_TOKEN to the same
# value NautGate has (env NAUTGATE_INGEST_TOKEN) or the gateway rejects it (401).
# Override the target with NAUTGATE_INGEST_URL (default local :8090).
#
# Usage:
#   scripts/codex-proxy.sh start    # launch mitmdump + addon (bg), print env to use
#   scripts/codex-proxy.sh stop
#   scripts/codex-proxy.sh status
#   scripts/codex-proxy.sh env      # print the export lines codexps needs
#   scripts/codex-proxy.sh logs
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CORE_DIR="$(cd "$SCRIPT_DIR/.." && pwd)/core"
LOG_DIR="$(cd "$SCRIPT_DIR/.." && pwd)/logs"
LOG_FILE="$LOG_DIR/codex-proxy.log"
PID_FILE="$LOG_DIR/codex-proxy.pid"
PORT="${CODEX_PROXY_PORT:-8092}"
CA="$HOME/.mitmproxy/mitmproxy-ca-cert.pem"
mkdir -p "$LOG_DIR"

_running() { [[ -f "$PID_FILE" ]] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; }

start() {
    if _running; then echo "✓ already running (pid $(cat "$PID_FILE")) on :$PORT"; env_block; return; fi
    local ingest_url="${NAUTGATE_INGEST_URL:-http://localhost:8090/v1/ingest}"
    if [[ -z "${NAUTGATE_INGEST_TOKEN:-}" ]]; then
        echo "⚠ NAUTGATE_INGEST_TOKEN is not set — NautGate will reject ingest (401)."
        echo "  Set the same token here and on NautGate (:8090), then restart."
    fi
    echo "starting codex capture proxy on 127.0.0.1:${PORT} → ${ingest_url} ..."
    ( cd "$CORE_DIR" \
        && NAUTGATE_INGEST_URL="$ingest_url" \
           NAUTGATE_INGEST_TOKEN="${NAUTGATE_INGEST_TOKEN:-}" \
           NAUTPROXY_AGENT_ID="${NAUTPROXY_AGENT_ID:-codex}" \
           exec uv run mitmdump \
        -s proxy/codex_capture.py \
        --listen-host 127.0.0.1 --listen-port "$PORT" \
        --set stream_large_bodies=100m \
        --flow-detail 0 \
    ) >"$LOG_FILE" 2>&1 &
    echo $! > "$PID_FILE"
    # mitmdump writes the CA on first boot; wait for it.
    for _ in $(seq 1 20); do [[ -f "$CA" ]] && break; sleep 0.3; done
    sleep 0.5
    if _running; then echo "✓ up (pid $(cat "$PID_FILE"))"; env_block
    else echo "✗ failed to start — see $LOG_FILE"; tail -5 "$LOG_FILE"; exit 1; fi
}

stop() {
    if _running; then kill "$(cat "$PID_FILE")" 2>/dev/null || true; rm -f "$PID_FILE"; echo "✓ stopped";
    else echo "not running"; rm -f "$PID_FILE"; fi
}

status() {
    if _running; then echo "✓ running (pid $(cat "$PID_FILE")) on 127.0.0.1:$PORT"; else echo "DOWN"; fi
    [[ -f "$CA" ]] && echo "CA: $CA" || echo "CA: not generated yet (run start once)"
}

# The env Codex needs: route through the proxy + trust its CA. Codex reads all of
# these depending on which HTTP stack a sub-request uses, so set them all.
env_block() {
    cat <<EOF

  export HTTPS_PROXY=http://127.0.0.1:$PORT
  export HTTP_PROXY=http://127.0.0.1:$PORT
  export NODE_EXTRA_CA_CERTS=$CA
  export SSL_CERT_FILE=$CA
  export CODEX_CA_CERTIFICATE=$CA
EOF
}

case "${1:-status}" in
    start) start ;;
    stop) stop ;;
    restart) stop || true; start ;;
    status) status ;;
    env) env_block ;;
    logs) tail -f "$LOG_FILE" ;;
    *) echo "usage: $0 {start|stop|restart|status|env|logs}"; exit 1 ;;
esac
