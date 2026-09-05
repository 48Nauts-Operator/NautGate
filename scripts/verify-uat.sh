#!/usr/bin/env bash
# Read-only verification of the currently running Stargate UAT stack.
set -euo pipefail

remote_host=${NAUTGATE_UAT_HOST:-stargate}
remote_dir=${NAUTGATE_UAT_DIR:-/Users/sg1/DevHub_STG/factory/02-development/NautGate-staged}
compose_file=${NAUTGATE_UAT_COMPOSE:-deploy/compose.production.yml}
compose_env=${NAUTGATE_UAT_ENV:-deploy/profiles/stargate.env}
compose_project=${NAUTGATE_UAT_PROJECT:-nautgate-stargate}
core_port=${NAUTGATE_UAT_CORE_PORT:-18090}
router_port=${NAUTGATE_UAT_ROUTER_PORT:-18404}
lmstudio_url=${NAUTGATE_UAT_LMSTUDIO_URL:-http://cand0rians-mac-studio.tail138398.ts.net:1238}

remote="export PATH=/opt/homebrew/bin:\$PATH; cd '$remote_dir';"
compose="docker compose --env-file '$compose_env' -p '$compose_project' -f '$compose_file'"

echo "Target: UAT on $remote_host (project $compose_project)"
ssh "$remote_host" "$remote $compose ps"

echo "Core health"
ssh "$remote_host" "curl -fsS --max-time 5 'http://127.0.0.1:$core_port/health'"
echo

echo "Core readiness"
ssh "$remote_host" "curl -fsS --max-time 5 'http://127.0.0.1:$core_port/ready'"
echo

echo "NautRouter health"
ssh "$remote_host" "curl -fsS --max-time 5 'http://127.0.0.1:$router_port/health'"
echo

echo "LM Studio reachability from Core"
ssh "$remote_host" "export PATH=/opt/homebrew/bin:\$PATH; docker exec '${compose_project}-nautgate-core-1' python -c \"import urllib.request; print(urllib.request.urlopen('$lmstudio_url/v1/models', timeout=5).status)\""

echo "LM Studio reachability from NautRouter"
ssh "$remote_host" "export PATH=/opt/homebrew/bin:\$PATH; docker exec '${compose_project}-nautrouter-1' node -e \"fetch('$lmstudio_url/v1/models').then(r=>{console.log(r.status);process.exit(r.ok?0:1)}).catch(e=>{console.error(e.message);process.exit(1)})\""

echo "UAT verification passed. No services were changed."
