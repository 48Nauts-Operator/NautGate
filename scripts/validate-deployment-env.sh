#!/usr/bin/env bash
set -euo pipefail

env_file=${1:-}
if [[ -z "$env_file" || ! -f "$env_file" ]]; then
  echo "usage: $0 <deployment-env-file>" >&2
  exit 2
fi

read_value() {
  local key=$1
  awk -F= -v key="$key" '$1 == key { sub(/^[^=]*=/, ""); print; exit }' "$env_file"
}

required=(
  NAUTGATE_PROJECT_NAME
  NAUTGATE_CORE_IMAGE
  NAUTGATE_ROUTER_IMAGE
  NAUTGATE_POSTGRES_IMAGE
  NAUTGATE_DB_VOLUME
  LMSTUDIO_BASE_URL
  POSTGRES_PASSWORD
)

failed=0
for key in "${required[@]}"; do
  value=$(read_value "$key")
  if [[ -z "$value" || "$value" == "REQUIRED" ]]; then
    echo "MISSING: $key"
    failed=1
  else
    echo "OK: $key"
  fi
done

lmstudio_url=$(read_value LMSTUDIO_BASE_URL)
if [[ -n "$lmstudio_url" && ! "$lmstudio_url" =~ ^https?://[^/[:space:]]+:[0-9]+/?$ ]]; then
  echo "INVALID: LMSTUDIO_BASE_URL must be an http(s) origin with an explicit port" >&2
  failed=1
fi

provider_found=0
for key in ANTHROPIC_API_KEY OPENAI_API_KEY GEMINI_API_KEY OPENROUTER_API_KEY; do
  if [[ -n "$(read_value "$key")" ]]; then
    provider_found=1
  fi
done
if (( provider_found )); then
  echo "OK: at least one provider key is configured"
else
  echo "INFO: no provider key is present; this is valid only if encrypted keys are already stored in the migrated database"
fi

if (( failed )); then
  echo "Deployment environment is incomplete. Values were not printed." >&2
  exit 1
fi
echo "Deployment environment contains all required keys. Values were not printed."
