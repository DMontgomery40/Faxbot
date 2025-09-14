#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
ENV_FILE="$ROOT_DIR/.env"

source "$ROOT_DIR/scripts/load-env.sh"

function set_env() {
  local key="$1" val="$2"
  if [[ ! -f "$ENV_FILE" ]]; then
    echo "$key=$val" >> "$ENV_FILE"; return
  fi
  if grep -qE "^${key}=" "$ENV_FILE"; then
    awk -v k="$key" -v v="$val" 'BEGIN{OFS=FS="="} $1==k{$0=k"="v} {print}' "$ENV_FILE" > "$ENV_FILE.tmp" && mv "$ENV_FILE.tmp" "$ENV_FILE"
  else
    echo "$key=$val" >> "$ENV_FILE"
  fi
}

echo "[i] Bootstrapping inbound in $ENV_FILE"
set_env INBOUND_ENABLED true

if [[ -z "${ASTERISK_INBOUND_SECRET:-}" ]]; then
  # shellcheck disable=SC2005
  SECRET=$(python3 - << 'PY'
import secrets
print(secrets.token_urlsafe(24))
PY
)
  set_env ASTERISK_INBOUND_SECRET "$SECRET"
  echo "[i] Generated ASTERISK_INBOUND_SECRET"
else
  set_env ASTERISK_INBOUND_SECRET "$ASTERISK_INBOUND_SECRET"
fi

if [[ -z "${REQUIRE_API_KEY:-}" ]]; then
  set_env REQUIRE_API_KEY true
fi

if [[ -z "${API_KEY:-}" ]]; then
  BOOTSTRAP=$(python3 - << 'PY'
import secrets
print('bootstrap_' + secrets.token_urlsafe(18))
PY
)
  set_env API_KEY "$BOOTSTRAP"
  echo "[i] Set bootstrap API_KEY"
fi

echo "[i] Updated .env. Restarting API if using docker compose..."
if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
  make up
else
  echo "[!] Docker not detected; start the API manually or use scripts/run-uvicorn-dev.sh" >&2
fi

echo "[i] Running inbound internal smoke test"
API_KEY=$(grep -E '^API_KEY=' "$ENV_FILE" | cut -d= -f2-) ASTERISK_INBOUND_SECRET=$(grep -E '^ASTERISK_INBOUND_SECRET=' "$ENV_FILE" | cut -d= -f2-) \
  "$ROOT_DIR/scripts/inbound-internal-smoke.sh"

echo "[✓] Inbound bootstrap complete"

