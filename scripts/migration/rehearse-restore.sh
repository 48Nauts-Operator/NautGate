#!/usr/bin/env bash
# Restore-test a custom-format NautGate dump using an isolated Docker volume.
set -euo pipefail

execute=0
keep_volume=0
backup=""
volume=""

usage() {
  cat <<'EOF'
usage: rehearse-restore.sh --backup ABSOLUTE_PATH --volume NEW_VOLUME [--execute] [--keep-volume]

Default is a read-only plan. The rehearsal publishes no ports and refuses an
existing volume. Unless --keep-volume is supplied, both container and volume
are removed after validation.
EOF
}

while (( $# )); do
  case "$1" in
    --backup) backup=${2:?missing backup}; shift 2 ;;
    --volume) volume=${2:?missing volume}; shift 2 ;;
    --execute) execute=1; shift ;;
    --keep-volume) keep_volume=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
done

[[ "$backup" == /* && -f "$backup" && -n "$volume" ]] || { usage >&2; exit 2; }
[[ "$volume" =~ ^[a-zA-Z0-9][a-zA-Z0-9_.-]+$ ]] || { echo "invalid volume name" >&2; exit 2; }
container="${volume}-restore"

docker volume inspect "$volume" >/dev/null 2>&1 && {
  echo "refusing existing volume: $volume" >&2
  exit 1
}
docker inspect "$container" >/dev/null 2>&1 && {
  echo "refusing existing container: $container" >&2
  exit 1
}

echo "Restore rehearsal:"
echo "  archive:   $backup"
echo "  volume:    $volume"
echo "  container: $container"
echo "  ports:     none"
if (( ! execute )); then
  echo "DRY RUN: no volume or container was created."
  exit 0
fi

backup_dir=$(dirname "$backup")
backup_name=$(basename "$backup")
created_volume=0
cleanup() {
  docker rm -f "$container" >/dev/null 2>&1 || true
  if (( created_volume && ! keep_volume )); then
    docker volume rm "$volume" >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT

shasum -a 256 "$backup"
docker run --rm -v "$backup_dir:/backup:ro" postgres:16 pg_restore -l "/backup/$backup_name" >/dev/null
docker volume create "$volume" >/dev/null
created_volume=1
docker run -d --name "$container" --restart=no \
  -e POSTGRES_USER=nautgate -e POSTGRES_PASSWORD=nautgate -e POSTGRES_DB=nautgate \
  -v "$volume:/var/lib/postgresql/data" -v "$backup_dir:/backup:ro" postgres:16 >/dev/null

n=0
until docker exec "$container" pg_isready -U nautgate -d nautgate >/dev/null 2>&1; do
  n=$((n + 1)); (( n < 60 )) || { echo "PostgreSQL did not become ready" >&2; exit 1; }
  sleep 1
done
[[ -z "$(docker port "$container")" ]] || { echo "rehearsal unexpectedly published a port" >&2; exit 1; }
docker exec "$container" pg_restore -U nautgate -d nautgate --no-owner --no-privileges --exit-on-error "/backup/$backup_name"
docker exec "$container" psql -U nautgate -d nautgate -Atc \
  "SELECT current_database(), pg_database_size(current_database()); SELECT count(*) FROM pg_tables WHERE schemaname='nautgate'; SELECT 'route_decisions', count(*) FROM nautgate.route_decisions;"
echo "Restore rehearsal passed."
