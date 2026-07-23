#!/usr/bin/env bash
# nautproxy-setup — one-time trust for the capture sidecar.
#
# The nautproxy sidecar is a TLS-terminating forward proxy, so a client only
# talks through it once it trusts the proxy's CA. This does the one-time bit:
#   1. pull the CA out of the running sidecar container,
#   2. trust it (macOS login keychain, no sudo; on Linux it prints the command),
#   3. print the env block to point a client (Codex etc.) at the proxy.
#
# Usage:
#   scripts/nautproxy-setup.sh            # extract + trust + print env
#   scripts/nautproxy-setup.sh env        # just print the env block
#   scripts/nautproxy-setup.sh --out PATH # where to write the CA (default ./nautproxy-ca.pem)
#
# Assumes `docker compose --profile proxy up -d` is running (service: nautproxy).
# Override the compose file with COMPOSE_FILE, the port with NAUTPROXY_PORT.
set -euo pipefail

PORT="${NAUTPROXY_PORT:-8092}"
SERVICE="nautproxy"
CA_IN_CONTAINER="/home/mitmproxy/.mitmproxy/mitmproxy-ca-cert.pem"
OUT="./nautproxy-ca.pem"
COMPOSE=(docker compose ${COMPOSE_FILE:+-f "$COMPOSE_FILE"})

# crude arg parse: [action] [--out PATH]
ACTION="setup"
while [[ $# -gt 0 ]]; do
    case "$1" in
        env) ACTION="env"; shift ;;
        --out) OUT="$2"; shift 2 ;;
        -h|--help) sed -n '2,17p' "$0"; exit 0 ;;
        *) echo "unknown arg: $1" >&2; exit 1 ;;
    esac
done

env_block() {
    local ca_abs
    ca_abs="$(cd "$(dirname "$OUT")" && pwd)/$(basename "$OUT")"
    cat <<EOF

Point a client at the proxy and trust the CA (Codex reads all of these):

  export HTTPS_PROXY=http://127.0.0.1:${PORT}
  export HTTP_PROXY=http://127.0.0.1:${PORT}
  export NODE_EXTRA_CA_CERTS=${ca_abs}   # Node / Claude Code
  export SSL_CERT_FILE=${ca_abs}         # Python / requests
  export REQUESTS_CA_BUNDLE=${ca_abs}
  export CODEX_CA_CERTIFICATE=${ca_abs}
  export CODEX_NETWORK_PROXY_ACTIVE=1    # Codex only honours the proxy with this

Then start the client in that shell; captured turns appear in the Audit Log.
EOF
}

if [[ "$ACTION" == "env" ]]; then
    env_block
    exit 0
fi

echo "→ extracting CA from the ${SERVICE} sidecar ..."
if ! "${COMPOSE[@]}" cp "${SERVICE}:${CA_IN_CONTAINER}" "$OUT" 2>/dev/null; then
    echo "✗ couldn't copy the CA from the ${SERVICE} container." >&2
    echo "  Is it up?  docker compose --profile proxy up -d" >&2
    exit 1
fi
echo "✓ CA written to $OUT"

case "$(uname -s)" in
    Darwin)
        echo "→ trusting the CA in your login keychain (no sudo; may prompt once) ..."
        if security add-trusted-cert -r trustRoot \
            -k "$HOME/Library/Keychains/login.keychain-db" "$OUT" 2>/dev/null; then
            echo "✓ trusted"
        else
            echo "⚠ automatic trust didn't complete. Trust it manually:"
            echo "    security add-trusted-cert -r trustRoot -k ~/Library/Keychains/login.keychain-db $OUT"
        fi
        ;;
    Linux)
        echo "→ on Linux, trust it system-wide (needs sudo):"
        echo "    sudo cp $OUT /usr/local/share/ca-certificates/nautproxy.crt && sudo update-ca-certificates"
        ;;
    *)
        echo "→ trust $OUT in your OS trust store (platform not auto-handled)."
        ;;
esac

env_block
