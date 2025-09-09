#!/usr/bin/env bash
set -euo pipefail

# Demo script that hits a running Faxbot API on localhost:8080
# 1) Creates a DB-backed API key via admin endpoint
# 2) Sends a test fax using a small TXT file

API_URL="${FAX_API_URL:-http://localhost:8080}"
ADMIN_KEY="${API_KEY:-bootstrap_admin_only}"

echo "[i] Checking API health at $API_URL/health"
curl -fsS "$API_URL/health" >/dev/null || { echo "[x] API not reachable at $API_URL"; exit 1; }

echo "[i] Creating API key via admin endpoint"
CREATE_BODY='{"name":"demo","owner":"local","scopes":["fax:send","fax:read"]}'

JSON=$(curl -fsS -X POST "$API_URL/admin/api-keys" \
  -H "X-API-Key: $ADMIN_KEY" \
  -H 'Content-Type: application/json' \
  -d "$CREATE_BODY")

if command -v jq >/dev/null 2>&1; then
  TOKEN=$(echo "$JSON" | jq -r .token)
else
  TOKEN=$(python3 - << 'PY'
import sys, json
print(json.load(sys.stdin).get('token',''))
PY
  << EOF
  $JSON
EOF
)
fi

if [[ -z "$TOKEN" || "$TOKEN" == "null" ]]; then
  echo "[x] Failed to create API key. Response: $JSON" >&2
  exit 1
fi
echo "[i] Created key: $TOKEN"

TMPFILE="$(mktemp /tmp/faxbot_demo_XXXXXX)"
echo "hello from faxbot" > "$TMPFILE"

echo "[i] Sending fax (test number +15551234567)"
SEND_JSON=$(curl -fsS -X POST "$API_URL/fax" \
  -H "X-API-Key: $TOKEN" \
  -F to=+15551234567 \
  -F "file=@$TMPFILE;type=text/plain")

JOB_ID=$(echo "$SEND_JSON" | { jq -r .id 2>/dev/null || python3 - << 'PY'
import sys, json
print(json.load(sys.stdin).get('id',''))
PY
  << EOF
  $SEND_JSON
EOF
})

echo "[i] Send response: $SEND_JSON"

if [[ -z "$JOB_ID" || "$JOB_ID" == "null" ]]; then
  echo "[x] No job id in response" >&2
  exit 1
fi

echo "[i] Fetching status for job $JOB_ID"
curl -fsS "$API_URL/fax/$JOB_ID" -H "X-API-Key: $TOKEN" | sed -e 's/^/[status] /'

echo "[✓] Done"
