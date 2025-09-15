#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
source "$ROOT_DIR/scripts/load-env.sh"

API_URL="${FAX_API_URL:-http://localhost:8080}"
ADMIN_KEY="${API_KEY:-}"
SECRET="${ASTERISK_INBOUND_SECRET:-}"

if [[ -z "$SECRET" ]]; then
  echo "[x] ASTERISK_INBOUND_SECRET not set. Set it in .env or export ASTERISK_INBOUND_SECRET." >&2
  exit 1
fi

echo "[i] Checking API health at $API_URL/health"
curl -fsS "$API_URL/health" >/dev/null || { echo "[x] API not reachable"; exit 1; }

if [[ -z "$ADMIN_KEY" ]]; then
  echo "[x] API_KEY (admin) is required to mint a read token for listing inbound." >&2
  exit 1
fi

echo "[i] Creating inbound read key via admin endpoint"
CREATE_BODY='{"name":"inbound-smoke","owner":"local","scopes":["inbound:list","inbound:read"]}'
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

echo "[i] Posting internal inbound event (simulated TIFF)"
# Try to create a TIFF placeholder inside the API container's /faxdata volume
USE_SIMULATE=0
TMPTIFF="/faxdata/inbound_smoke_$(date +%s).tiff"
if command -v docker >/dev/null 2>&1 && docker compose ps api >/dev/null 2>&1; then
  if docker compose exec -T api sh -c "echo -n TIFF_PLACEHOLDER > '$TMPTIFF'" >/dev/null 2>&1; then
    : # created
  else
    USE_SIMULATE=1
  fi
else
  USE_SIMULATE=1
fi

if [[ "$USE_SIMULATE" -eq 0 ]]; then
  PAYLOAD=$(cat <<JSON
{ "tiff_path": "$TMPTIFF", "to_number": "+15551234567", "from_number": "+15550001111", "faxstatus": "received", "faxpages": 1, "uniqueid": "demo-$(date +%s)" }
JSON
  )
  POST_JSON=$(curl -fsS -X POST "$API_URL/_internal/asterisk/inbound" \
    -H "X-Internal-Secret: $SECRET" \
    -H 'Content-Type: application/json' \
    -d "$PAYLOAD")
else
  echo "[i] Could not write to /faxdata in container; using admin simulate endpoint"
  POST_JSON=$(curl -fsS -X POST "$API_URL/admin/inbound/simulate" -H "X-API-Key: $ADMIN_KEY" -H 'Content-Type: application/json' -d '{}')
fi
ID=$(echo "$POST_JSON" | { jq -r .id 2>/dev/null || python3 - << 'PY'
import sys, json
print(json.load(sys.stdin).get('id',''))
PY
  << EOF
  $POST_JSON
EOF
})

echo "[i] Inbound created: $ID"

echo "[i] Listing inbound"
curl -fsS "$API_URL/inbound" -H "X-API-Key: $TOKEN" | sed -e 's/^/[inbound] /'

echo "[i] Downloading PDF"
curl -fsS "$API_URL/inbound/$ID/pdf" -H "X-API-Key: $TOKEN" -o "/tmp/inbound_$ID.pdf"
echo "[✓] Saved /tmp/inbound_$ID.pdf"
