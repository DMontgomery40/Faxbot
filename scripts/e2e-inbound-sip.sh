#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
source "$ROOT_DIR/scripts/load-env.sh"

API_URL="${FAX_API_URL:-http://localhost:8080}"
ADMIN_KEY="${API_KEY:-}"

LOWER_INBOUND=$(printf %s "${INBOUND_ENABLED:-}" | tr '[:upper:]' '[:lower:]')
if [[ -z "${INBOUND_ENABLED:-}" || "$LOWER_INBOUND" != "true" ]]; then
  echo "[x] INBOUND_ENABLED must be true in .env or exported" >&2
  exit 1
fi
if [[ -z "${ASTERISK_INBOUND_SECRET:-}" ]]; then
  echo "[x] ASTERISK_INBOUND_SECRET must be set" >&2
  exit 1
fi

echo "[i] Checking API health at $API_URL/health"
curl -fsS "$API_URL/health" >/dev/null || { echo "[x] API not reachable"; exit 1; }

echo "[i] Checking Asterisk registration"
if command -v docker >/dev/null 2>&1; then
  if docker compose ps asterisk >/dev/null 2>&1; then
    docker compose exec -T asterisk asterisk -rx "pjsip show registrations" || true
  else
    echo "[!] docker compose ps asterisk failed; ensure the stack is running" >&2
  fi
else
  echo "[!] docker not found; skipping Asterisk check" >&2
fi

if [[ -z "$ADMIN_KEY" ]]; then
  echo "[x] API_KEY (admin) is required to mint an inbound read token" >&2
  exit 1
fi

echo "[i] Creating inbound read key via admin endpoint"
CREATE_BODY='{"name":"inbound-read","owner":"local","scopes":["inbound:list","inbound:read"]}'
JSON=$(curl -fsS -X POST "$API_URL/admin/api-keys" \
  -H "X-API-Key: $ADMIN_KEY" \
  -H 'Content-Type: application/json' \
  -d "$CREATE_BODY")

TOKEN=$(echo "$JSON" | { jq -r .token 2>/dev/null || python3 - << 'PY'
import sys, json
print(json.load(sys.stdin).get('token',''))
PY
  << EOF
  $JSON
EOF
})
if [[ -z "$TOKEN" || "$TOKEN" == "null" ]]; then
  echo "[x] Failed to create API key. Response: $JSON" >&2
  exit 1
fi

DID="${SIP_FROM_USER:-}"
echo "[i] Ready to receive. Send a fax now to your DID: ${DID:-<set SIP_FROM_USER>}"
echo "    Monitoring /inbound for a new item (press Ctrl+C to stop)"

START_TS=$(date +%s)
LAST_COUNT=0
while true; do
  OUT=$(curl -fsS "$API_URL/inbound" -H "X-API-Key: $TOKEN" || true)
  COUNT=$(echo "$OUT" | { jq 'length' 2>/dev/null || python3 - << 'PY'
import sys, json
try:
  print(len(json.load(sys.stdin)))
except Exception:
  print(0)
PY
  << EOF
  $OUT
EOF
})
  if [[ "$COUNT" -gt "$LAST_COUNT" ]]; then
    ID=$(echo "$OUT" | { jq -r '.[0].id' 2>/dev/null || python3 - << 'PY'
import sys, json
data=json.load(sys.stdin)
print(data[0]['id'] if data else '')
PY
      << EOF
      $OUT
EOF
    })
    echo "[✓] New inbound detected: $ID"
    echo "[i] Downloading PDF"
    curl -fsS "$API_URL/inbound/$ID/pdf" -H "X-API-Key: $TOKEN" -o "/tmp/inbound_$ID.pdf"
    echo "[✓] Saved /tmp/inbound_$ID.pdf"
    exit 0
  fi
  if (( $(date +%s) - START_TS > 600 )); then
    echo "[x] Timeout after 10 minutes waiting for inbound" >&2
    exit 1
  fi
  sleep 5
done
