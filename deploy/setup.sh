#!/bin/sh
# NautGate capture setup — trust the proxy CA (via a file, no sudo) and install a
# `codex` shell wrapper so your Codex traffic is captured. Re-runnable.
#
#   curl -fsSL https://nautgate.dev/setup.sh | sh
#
# Needs the nautproxy sidecar running first:
#   docker compose --profile proxy up -d
#
# Works on macOS, Linux, and Windows/WSL. Native Windows PowerShell: setup.ps1.
set -eu

PROXY_URL="http://${NAUTPROXY_HOST:-127.0.0.1}:${NAUTPROXY_PORT:-8092}"
NG_DIR="$HOME/.nautgate"
CA="$NG_DIR/nautproxy-ca.pem"

tty_read() {  # read one line from the real terminal (stdin is the curl pipe).
    # The group redirect swallows the "Device not configured" the OPEN of
    # /dev/tty raises when there's no controlling terminal (CI, curl in a pipe).
    REPLY=""
    { IFS= read -r REPLY < /dev/tty; } 2>/dev/null || REPLY=""
}
prompt() { { printf '%s ' "$1" > /dev/tty; } 2>/dev/null || true; tty_read; }

echo "NautGate capture setup"
echo "----------------------"

# 1. Fetch the CA through the running proxy — mitmproxy serves it at mitm.it,
#    so this needs no repo checkout and no docker cp.
mkdir -p "$NG_DIR"
echo "→ fetching the proxy CA from ${PROXY_URL} ..."
if ! curl -fsS -x "$PROXY_URL" http://mitm.it/cert/pem -o "$CA" 2>/dev/null; then
    echo "✗ couldn't reach the proxy at ${PROXY_URL}."
    echo "  Start it first:  docker compose --profile proxy up -d"
    exit 1
fi
grep -q "BEGIN CERTIFICATE" "$CA" || { echo "✗ that didn't look like a certificate."; exit 1; }
echo "✓ CA saved to ${CA}"

# 2. Pick the shell rc file.
RC="${NAUTGATE_RC:-}"
if [ -z "$RC" ]; then
    case "$(basename "${SHELL:-sh}")" in
        zsh)  RC="$HOME/.zshrc" ;;
        bash) RC="$HOME/.bashrc" ;;
        *)    RC="$HOME/.profile" ;;
    esac
fi

# 3. Mode — auto-approve (--yolo) is off by default.
prompt "Auto-approve edits/commands (codex --yolo)? [y/N]"
FLAGS=""
case "$REPLY" in y | Y | yes | YES) FLAGS=" -s workspace-write --yolo" ;; esac

# The wrapper trusts the CA via env (file), so no OS trust store is needed for
# Codex. `command codex` avoids recursing into this function.
BLOCK="$(cat <<EOF
# >>> NautGate capture >>>
codex() {
  local _ng_ca="$CA"
  HTTPS_PROXY=$PROXY_URL HTTP_PROXY=$PROXY_URL CODEX_NETWORK_PROXY_ACTIVE=1 \\
  NODE_EXTRA_CA_CERTS="\$_ng_ca" SSL_CERT_FILE="\$_ng_ca" CODEX_CA_CERTIFICATE="\$_ng_ca" \\
  command codex "\$@"$FLAGS
}
# <<< NautGate capture <<<
EOF
)"

echo ""
echo "This will add a codex() wrapper to ${RC}:"
echo "$BLOCK"
prompt "Add it? [Y/n]"
case "$REPLY" in n | N | no | NO) echo "skipped — CA is still at ${CA}."; exit 0 ;; esac

touch "$RC"
if grep -q ">>> NautGate capture >>>" "$RC"; then
    echo "✓ a NautGate codex() is already in ${RC} — edit/remove that block to change it."
else
    printf '\n%s\n' "$BLOCK" >> "$RC"
    echo "✓ added to ${RC}"
fi
echo ""
echo "Done. Run:  source ${RC}    then just use:  codex"
echo "Your turns will appear live in the NautGate Audit Log."
