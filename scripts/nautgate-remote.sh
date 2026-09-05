#!/usr/bin/env bash
# Remote-aware operator commands. This script never starts a local NautGate.
set -euo pipefail

remote_host=${NAUTGATE_REMOTE_HOST:-stargate}
remote_dir=${NAUTGATE_REMOTE_DIR:-/Users/sg1/DevHub_STG/factory/02-development/NautGate-staged}
compose_file=${NAUTGATE_REMOTE_COMPOSE:-compose.migration.yml}
remote_port=${NAUTGATE_REMOTE_PORT:-18090}
local_port=${NAUTGATE_LOCAL_PORT:-18091}
control_socket=${NAUTGATE_TUNNEL_SOCKET:-$HOME/.ssh/nautgate-stargate.sock}

usage() {
  cat <<EOF
usage: $0 status|logs|tunnel-status|tunnel-start|tunnel-stop

Defaults use rehearsal port $local_port. Existing localhost:8090 aliases are
not modified. Set NAUTGATE_LOCAL_PORT=8090 only during an approved cutover.
EOF
}

remote_compose() {
  ssh "$remote_host" "export PATH=/opt/homebrew/bin:\$PATH; cd '$remote_dir'; docker compose -f '$compose_file' $*"
}

case ${1:-status} in
  status)
    echo "Remote containers:"
    remote_compose ps
    echo
    echo "Remote readiness:"
    ssh "$remote_host" "curl -fsS --max-time 5 'http://127.0.0.1:$remote_port/ready'" || true
    echo
    "$0" tunnel-status
    ;;
  logs)
    shift
    remote_compose "logs --tail=200 ${*:-}"
    ;;
  tunnel-status)
    if ssh -S "$control_socket" -O check "$remote_host" >/dev/null 2>&1; then
      echo "Tunnel is active: 127.0.0.1:$local_port -> $remote_host:127.0.0.1:$remote_port"
    else
      echo "Tunnel is stopped"
      exit 1
    fi
    ;;
  tunnel-start)
    if ssh -S "$control_socket" -O check "$remote_host" >/dev/null 2>&1; then
      echo "Tunnel is already active"
      exit 0
    fi
    ssh -fNT \
      -M -S "$control_socket" \
      -o ExitOnForwardFailure=yes \
      -o ServerAliveInterval=30 \
      -o ServerAliveCountMax=3 \
      -L "127.0.0.1:$local_port:127.0.0.1:$remote_port" \
      "$remote_host"
    "$0" tunnel-status
    ;;
  tunnel-stop)
    ssh -S "$control_socket" -O exit "$remote_host"
    ;;
  -h|--help) usage ;;
  *) usage >&2; exit 2 ;;
esac

