#!/usr/bin/env bash
set -euo pipefail

lock_file=${1:-}
if [[ -z "$lock_file" || ! -f "$lock_file" ]]; then
  echo "usage: $0 <image-lock-file>" >&2
  exit 2
fi

# shellcheck disable=SC1090
source "$lock_file"

check_image() {
  local label=$1 image=$2 expected=$3 actual
  if [[ -z "$image" || -z "$expected" ]]; then
    echo "ERROR: $label image name or expected ID is empty" >&2
    return 1
  fi
  actual=$(docker image inspect "$image" --format '{{.Id}}' 2>/dev/null) || {
    echo "ERROR: $label image is not present locally: $image" >&2
    return 1
  }
  if [[ "$actual" != "$expected" ]]; then
    echo "ERROR: $label image ID mismatch for $image" >&2
    echo "  expected: $expected" >&2
    echo "  actual:   $actual" >&2
    return 1
  fi
  echo "OK: $label $image $actual"
}

check_image core "$NAUTGATE_CORE_IMAGE" "$NAUTGATE_CORE_IMAGE_ID"
check_image router "$NAUTGATE_ROUTER_IMAGE" "$NAUTGATE_ROUTER_IMAGE_ID"
check_image postgres "$NAUTGATE_POSTGRES_IMAGE" "$NAUTGATE_POSTGRES_IMAGE_ID"
