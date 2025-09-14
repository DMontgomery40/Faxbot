#!/usr/bin/env bash
set -euo pipefail

# One-command helper to:
# 1) Start an HTTPS tunnel to local API (prefers cloudflared, falls back to ngrok)
# 2) Discover the public URL
# 3) Update .env (PUBLIC_API_URL, PHAXIO_CALLBACK_URL, FAX_BACKEND=phaxio)
# 4) Restart the API container via make up-cloud

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT_DIR"

ENV_FILE="${ROOT_DIR}/.env"

function set_env() {
  local key="$1" val="$2"
  if [[ ! -f "$ENV_FILE" ]]; then
    echo "$key=$val" >> "$ENV_FILE"
    return
  fi
  if grep -qE "^${key}=" "$ENV_FILE"; then
    # Replace line
    awk -v k="$key" -v v="$val" 'BEGIN{OFS=FS="="} $1==k{$0=k"="v} {print}' "$ENV_FILE" > "$ENV_FILE.tmp" && mv "$ENV_FILE.tmp" "$ENV_FILE"
  else
    echo "$key=$val" >> "$ENV_FILE"
  fi
}

function have() { command -v "$1" >/dev/null 2>&1; }

function start_cloudflared() {
  echo "[setup] Starting cloudflared quick tunnel to http://localhost:8080" >&2
  # Start in background; capture output
  local log
  log="$(mktemp -t cloudflared.XXXXXX.log)"
  cloudflared tunnel --url http://localhost:8080 --no-autoupdate --logfile "$log" --loglevel info &
  local pid=$!
  echo "$pid" > "$log.pid"
  # Wait for URL to appear
  local url=""
  for i in {1..40}; do
    # Look for https://*.trycloudflare.com
    if grep -Eo "https://[a-zA-Z0-9.-]+\.trycloudflare\.com" "$log" | tail -1 >/tmp/._cf_url; then
      url="$(cat /tmp/._cf_url)"
      if [[ -n "$url" ]]; then
        echo "$url"
        return 0
      fi
    fi
    sleep 0.5
  done
  echo "" # none
  return 1
}

function start_ngrok() {
  echo "[setup] Starting ngrok http 8080 (requires authtoken)" >&2
  ngrok http 8080 >/dev/null 2>&1 &
  local pid=$!
  # poll 4040 for tunnels
  local url=""
  for i in {1..40}; do
    if curl -sS http://127.0.0.1:4040/api/tunnels >/dev/null 2>&1; then
      url=$(curl -sS http://127.0.0.1:4040/api/tunnels | jq -r '.tunnels[]?.public_url' | grep -E '^https://' | head -1 || true)
      if [[ -n "$url" ]]; then
        echo "$url"
        return 0
      fi
    fi
    sleep 0.5
  done
  echo ""
  return 1
}

public_url=""
if have cloudflared; then
  set +e
  public_url=$(start_cloudflared) || true
  set -e
elif have ngrok; then
  set +e
  public_url=$(start_ngrok) || true
  set -e
else
  echo "[error] Neither cloudflared nor ngrok is installed. Install one and re-run." >&2
  exit 1
fi

if [[ -z "$public_url" ]]; then
  echo "[error] Failed to obtain a public HTTPS URL." >&2
  exit 1
fi

echo "[setup] Public URL: $public_url" >&2

# Update .env
set_env FAX_BACKEND phaxio
set_env PUBLIC_API_URL "$public_url"
set_env PHAXIO_CALLBACK_URL "$public_url/phaxio-callback"

echo "[setup] Updated .env with PUBLIC_API_URL and PHAXIO_CALLBACK_URL" >&2

# Restart cloud API only
if have docker && docker compose version >/dev/null 2>&1; then
  echo "[setup] Restarting Faxbot API (cloud-only)" >&2
  make down >/dev/null 2>&1 || true
  make up-cloud
  echo "[setup] API restarted with new PUBLIC_API_URL" >&2
else
  echo "[note] Docker not detected. Start API separately (make up-cloud)." >&2
fi

echo "[done] Use scripts/send-fax.sh to send a test fax to your Phaxio number." >&2

