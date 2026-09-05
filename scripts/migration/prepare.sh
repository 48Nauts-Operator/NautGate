#!/usr/bin/env bash
# Prepare a point-in-time NautGate database copy on another Docker host.
# The default mode is a read-only plan. --execute is required for writes.
set -euo pipefail

execute=0
target_host=""
target_dir=""
target_volume=""
source_container=${SOURCE_DB_CONTAINER:-nautgate-db}
source_health_url=${SOURCE_HEALTH_URL:-http://127.0.0.1:8090/ready}

usage() {
  cat <<'EOF'
usage: prepare.sh --target-host HOST --target-dir ABSOLUTE_PATH \
  --target-volume NEW_VOLUME [--source-container NAME] [--execute]

Without --execute, performs read-only preflight checks and prints the plan.
Execution never stops the source and never publishes a target port. It refuses
to overwrite an existing backup file, volume, or restore container.
EOF
}

while (( $# )); do
  case "$1" in
    --execute) execute=1; shift ;;
    --target-host) target_host=${2:?missing target host}; shift 2 ;;
    --target-dir) target_dir=${2:?missing target directory}; shift 2 ;;
    --target-volume) target_volume=${2:?missing target volume}; shift 2 ;;
    --source-container) source_container=${2:?missing source container}; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
done

[[ -n "$target_host" && "$target_dir" == /* && -n "$target_volume" ]] || {
  usage >&2
  exit 2
}
[[ "$target_volume" =~ ^[a-zA-Z0-9][a-zA-Z0-9_.-]+$ ]] || {
  echo "invalid target volume name" >&2
  exit 2
}

for command in curl docker ssh; do
  command -v "$command" >/dev/null || { echo "missing command: $command" >&2; exit 1; }
done

echo "Checking source readiness..."
curl -fsS "$source_health_url" >/dev/null
docker inspect "$source_container" >/dev/null
docker exec "$source_container" pg_isready -U nautgate -d nautgate >/dev/null
source_bytes=$(docker exec "$source_container" psql -U nautgate -d nautgate -Atc \
  'SELECT pg_database_size(current_database())')

echo "Checking target without changing it..."
ssh "$target_host" "export PATH=/opt/homebrew/bin:\$PATH; colima status >/dev/null; DOCKER_CONTEXT=colima docker volume inspect '$target_volume' >/dev/null 2>&1 && exit 1 || exit 0"
ssh "$target_host" "test ! -e '$target_dir'"
target_available_kb=$(ssh "$target_host" "export PATH=/opt/homebrew/bin:\$PATH; colima ssh -- df -Pk /var/lib/docker | tail -1 | tr -s ' ' | cut -d ' ' -f4")
target_available_bytes=$((target_available_kb * 1024))
# Keep enough room for both the compressed archive and restored database, with
# a conservative 2x database-size floor.
required_bytes=$((source_bytes * 2))
if (( target_available_bytes < required_bytes )); then
  echo "target capacity is insufficient: need at least $required_bytes bytes, have $target_available_bytes" >&2
  exit 1
fi

timestamp=$(date -u +%Y%m%dT%H%M%SZ)
backup_name="nautgate-${timestamp}.dump"
restore_container="${target_volume}-restore"

cat <<EOF
Preparation plan:
  source container: $source_container
  source DB bytes:  $source_bytes
  target host:      $target_host
  target free bytes:$target_available_bytes
  target directory: $target_dir
  backup archive:   $target_dir/backups/$backup_name
  target volume:    $target_volume
  target ports:     none
  source downtime:  none
EOF

if (( ! execute )); then
  echo "DRY RUN: no files, volumes, or containers were created."
  exit 0
fi

echo "Creating isolated target directory..."
ssh "$target_host" "umask 077; mkdir -p '$target_dir/backups'; test ! -e '$target_dir/backups/$backup_name'"

echo "Streaming transaction-consistent backup..."
set -o pipefail
docker exec "$source_container" pg_dump -U nautgate -d nautgate -Fc --compress=6 --no-owner --no-privileges \
  | ssh "$target_host" "umask 077; dd of='$target_dir/backups/$backup_name' bs=4m"

echo "Validating archive and creating isolated volume..."
ssh "$target_host" "set -e; export PATH=/opt/homebrew/bin:\$PATH; export DOCKER_CONTEXT=colima; \
  shasum -a 256 '$target_dir/backups/$backup_name'; \
  docker run --rm -v '$target_dir/backups:/backup:ro' postgres:16 pg_restore -l '/backup/$backup_name' >/dev/null; \
  docker volume create '$target_volume' >/dev/null; \
  docker run -d --name '$restore_container' --restart=no \
    -e POSTGRES_USER=nautgate -e POSTGRES_PASSWORD=nautgate -e POSTGRES_DB=nautgate \
    -v '$target_volume:/var/lib/postgresql/data' \
    -v '$target_dir/backups:/backup:ro' postgres:16 >/dev/null"

cleanup_restore() {
  ssh "$target_host" "export PATH=/opt/homebrew/bin:\$PATH; DOCKER_CONTEXT=colima docker rm -f '$restore_container' >/dev/null 2>&1 || true" || true
}
trap cleanup_restore EXIT

ssh "$target_host" "set -e; export PATH=/opt/homebrew/bin:\$PATH; export DOCKER_CONTEXT=colima; \
  n=0; until docker exec '$restore_container' pg_isready -U nautgate -d nautgate >/dev/null 2>&1; do \
    n=\$((n+1)); test \$n -lt 60; /bin/sleep 1; done; \
  test -z \"\$(docker port '$restore_container')\"; \
  docker exec '$restore_container' pg_restore -U nautgate -d nautgate \
    --no-owner --no-privileges --exit-on-error '/backup/$backup_name'; \
  docker exec '$restore_container' psql -U nautgate -d nautgate -Atc \
    \"SELECT current_database(), pg_database_size(current_database()); SELECT count(*) FROM pg_tables WHERE schemaname='nautgate';\""

cleanup_restore
trap - EXIT
echo "Preparation complete. The restored target volume is stopped and has never published a port."
