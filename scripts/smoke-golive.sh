#!/usr/bin/env bash
# Go-live smoke test (NAUTGATE-9): build the images, bring up the whole stack in
# an isolated throwaway project, and assert the things that must work before we
# ship. Exits non-zero on the first hard failure; always tears the stack down.
#
#   scripts/smoke-golive.sh
#
# Isolated by design: its own compose project, container names, volumes, and a
# core on :${HOST_PORT} (default 18099) — it never touches a live :8090 stack.
# Provider-key and offline checks are skipped gracefully when those features
# aren't on the branch under test, so this runs on the docker branch and on a
# fully-merged main alike.
set -uo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
PROJECT="ngsmoke"
HOST_PORT="${HOST_PORT:-18099}"
CORE_IMG="ghcr.io/48nauts-operator/nautgate:smoke"
ROUTER_IMG="ghcr.io/48nauts-operator/nautrouter:smoke"
WORK="$(mktemp -d)"
COMPOSE="$WORK/compose.yml"
B="http://127.0.0.1:${HOST_PORT}"
FAILED=0

pass() { printf "  \033[32m✓\033[0m %s\n" "$1"; }
fail() { printf "  \033[31m✗ %s\033[0m\n" "$1"; FAILED=$((FAILED+1)); }
info() { printf "\033[1m== %s\033[0m\n" "$1"; }

teardown() {
  info "teardown"
  docker compose -p "$PROJECT" -f "$COMPOSE" down -v >/dev/null 2>&1
  rm -rf "$WORK"
}
trap teardown EXIT

# ── build ──────────────────────────────────────────────────────────────────
info "build images"
docker build -q -f "$REPO/core/Dockerfile" -t "$CORE_IMG" "$REPO" >/dev/null \
  && pass "core image" || { fail "core build"; exit 1; }
docker build -q -f "$REPO/vendor/NautRouter/Dockerfile" -t "$ROUTER_IMG" "$REPO/vendor/NautRouter" >/dev/null \
  && pass "router image" || { fail "router build"; exit 1; }

# ── isolated stack ─────────────────────────────────────────────────────────
cat > "$COMPOSE" <<YAML
services:
  db:
    image: postgres:16
    environment: { POSTGRES_USER: nautgate, POSTGRES_PASSWORD: nautgate, POSTGRES_DB: nautgate }
    volumes: [ "dbdata:/var/lib/postgresql/data" ]
    healthcheck: { test: ["CMD-SHELL","pg_isready -U nautgate -d nautgate"], interval: 3s, timeout: 3s, retries: 20 }
  nautrouter:
    image: ${ROUTER_IMG}
    environment: { NAUT_PORT: "8404", NAUT_WS_PORT: "8403" }
    healthcheck: { test: ["CMD","curl","-fsS","http://localhost:8404/health"], interval: 5s, timeout: 3s, retries: 10, start_period: 5s }
  nautgate:
    image: ${CORE_IMG}
    depends_on:
      db: { condition: service_healthy }
      nautrouter: { condition: service_healthy }
    environment:
      NAUTGATE_DB_URL: postgres://nautgate:nautgate@db:5432/nautgate
      NAUTGATE_LISTEN_HOST: 0.0.0.0
      NAUTROUTER_BASE_URL: http://nautrouter:8404
      NAUTGATE_MASTER_KEY: smoke-master-key
    ports: [ "127.0.0.1:${HOST_PORT}:8090" ]
    volumes: [ "backups:/root/.nautgate/backups" ]
volumes: { dbdata: {}, backups: {} }
YAML

info "bring up stack"
docker compose -p "$PROJECT" -f "$COMPOSE" up -d >/dev/null 2>&1
CID="${PROJECT}-nautgate-1"
for _ in $(seq 1 45); do
  [ "$(docker inspect -f '{{.State.Health.Status}}' "$CID" 2>/dev/null)" = "healthy" ] && break
  sleep 2
done
[ "$(docker inspect -f '{{.State.Health.Status}}' "$CID" 2>/dev/null)" = "healthy" ] \
  && pass "stack healthy" || { fail "stack never became healthy"; docker logs "$CID" 2>&1 | tail -20; exit 1; }

LOG="$(docker logs "$CID" 2>&1)"

# ── assertions ─────────────────────────────────────────────────────────────
info "endpoints"
[ "$(curl -s -o /dev/null -w '%{http_code}' "$B/health")" = "200" ] && pass "/health 200" || fail "/health"
[ "$(curl -s -o /dev/null -w '%{http_code}' "$B/ready")" = "200" ] && pass "/ready 200" || fail "/ready"
[ "$(curl -s -o /dev/null -w '%{http_code}' "$B/dashboard")" = "200" ] && pass "/dashboard 200 (static shipped)" || fail "/dashboard"

info "config baked into the image (the NAUTGATE-5 blocker)"
echo "$LOG" | grep -q '"event": "routing_table_loaded"' && \
  echo "$LOG" | grep '"routing_table_loaded"' | grep -qv '"tiers": 0' \
  && pass "routing table loaded (non-empty)" || fail "routing table empty/missing"
echo "$LOG" | grep -q '"event": "pricing_table_loaded"' && \
  echo "$LOG" | grep '"pricing_table_loaded"' | grep -qv '"models": 0' \
  && pass "pricing table loaded (non-empty)" || fail "pricing table empty/missing — costs would be NULL"

info "first-run key (NAUTGATE-5)"
KEY="$(echo "$LOG" | grep -oE 'ng_[a-f0-9]{32}_[A-Za-z0-9_-]+' | head -1)"
if [ -z "$KEY" ]; then
  # Every later check sends `Bearer $KEY`; with an empty key they'd all 401 and
  # report one root cause as several failures. Fail once and bail to the verdict.
  fail "no first-run key in log — skipping key-dependent checks"
  echo; [ "$FAILED" -eq 0 ] && exit 0 || { printf "\033[31mSMOKE FAILED — %d check(s)\033[0m\n" "$FAILED"; exit 1; }
fi
pass "first-run key minted + printed to log"
[ "$(curl -s -o /dev/null -w '%{http_code}' -H "Authorization: Bearer $KEY" "$B/v1/whoami")" = "200" ] \
  && pass "key authenticates (whoami 200)" || fail "key did not authenticate"
[ "$(curl -s -o /dev/null -w '%{http_code}' "$B/v1/whoami")" = "401" ] \
  && pass "no-key rejected (401)" || fail "unauth request not rejected"

info "backups work in-container (NAUTGATE-7)"
BK="$(curl -s -X POST -H "Authorization: Bearer $KEY" "$B/v1/backups")"
echo "$BK" | grep -q '"status": *"ok"\|"status":"ok"' && pass "manual backup ok" || fail "backup failed: $(echo "$BK" | head -c 200)"
docker exec "$CID" sh -c 'f=$(ls -t /root/.nautgate/backups/*.sql.gz 2>/dev/null | head -1); [ -n "$f" ] && gzip -t "$f"' \
  && pass "dump landed in volume + valid gzip" || fail "no valid dump in the backups volume"
BID="$(curl -s -H "Authorization: Bearer $KEY" "$B/v1/backups" | python3 -c 'import sys,json;print(json.load(sys.stdin)["items"][0]["id"])' 2>/dev/null)"
if [ -n "$BID" ]; then
  curl -s -X POST -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" -d '{"confirm":true}' "$B/v1/backups/$BID/restore" >/dev/null
  [ "$(curl -s -o /dev/null -w '%{http_code}' -H "Authorization: Bearer $KEY" "$B/v1/whoami")" = "200" ] \
    && pass "destructive restore round-trips (key survives)" || fail "restore broke the schema"
else
  fail "could not read back the backup id"
fi

info "provider keys (NAUTGATE-8, skipped if not on this branch)"
PROV_CODE="$(curl -s -o /dev/null -w '%{http_code}' -H "Authorization: Bearer $KEY" "$B/v1/providers")"
if [ "$PROV_CODE" = "200" ]; then
  curl -s -X PUT -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
    -d '{"key":"sk-smoke-BOGUS-000"}' "$B/v1/providers/openrouter" >/dev/null
  curl -s -H "Authorization: Bearer $KEY" "$B/v1/providers" | grep -q '"source": *"db"\|"source":"db"' \
    && pass "provider key stored (source db)" || fail "provider key not stored"
  docker exec "${PROJECT}-db-1" psql -U nautgate -d nautgate -tAc \
    "SELECT encode(ciphertext,'escape') !~ 'BOGUS' FROM nautgate.provider_credentials WHERE provider='openrouter';" 2>/dev/null \
    | grep -q '^t' && pass "provider key encrypted at rest (no plaintext in DB)" || fail "plaintext leaked into DB"
else
  info "  (v1/providers → $PROV_CODE — NAUTGATE-8 not on this branch, skipping)"
fi

# ── verdict ────────────────────────────────────────────────────────────────
echo
if [ "$FAILED" -eq 0 ]; then
  printf "\033[32mSMOKE PASSED\033[0m\n"; exit 0
else
  printf "\033[31mSMOKE FAILED — %d check(s)\033[0m\n" "$FAILED"; exit 1
fi
