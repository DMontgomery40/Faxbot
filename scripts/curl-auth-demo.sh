#!/usr/bin/env bash
# Show the key flow against a running Faxbot with sending turned off:
# create a send/read key, queue a one-page fax, read its status, revoke the key.
# The fax is sent as queue-only, so Faxbot refuses it when sending is turned on:
# this demo never places a call.
#
#   API_KEY=<admin key> FAX_API_URL=http://localhost:8080 scripts/curl-auth-demo.sh
set -euo pipefail

API_URL="${FAX_API_URL:-http://localhost:8080}"
ADMIN_KEY="${API_KEY:-}"

json_field() { python3 -c 'import json,sys; print(json.load(sys.stdin).get(sys.argv[1]) or "")' "$1"; }

[[ -n "$ADMIN_KEY" ]] || { echo "[x] Set API_KEY to an admin key (System → Access → Keys)." >&2; exit 1; }
curl -fsS "$API_URL/health" >/dev/null || { echo "[x] Faxbot does not answer at $API_URL" >&2; exit 1; }

echo "[i] Creating a key that may send and read faxes"
CREATED=$(curl -fsS -X POST "$API_URL/admin/api-keys" -H "X-API-Key: $ADMIN_KEY" -H 'Content-Type: application/json' \
  -d '{"name":"curl-auth-demo","owner":"scripts","scopes":["fax:send","fax:read"]}')
KEY_ID=$(echo "$CREATED" | json_field key_id)
TOKEN=$(echo "$CREATED" | json_field token)
[[ -n "$TOKEN" ]] || { echo "[x] Faxbot did not create the key." >&2; exit 1; }
echo "[i] Created key ${TOKEN:0:20}… (shown shortened)"
trap 'curl -fsS -X DELETE "$API_URL/admin/api-keys/$KEY_ID" -H "X-API-Key: $ADMIN_KEY" >/dev/null && echo "[i] Key revoked"' EXIT

DOC="$(mktemp "${TMPDIR:-/tmp}/faxbot_demo_XXXXXX")"
echo "hello from faxbot" > "$DOC"

echo "[i] Queueing a fax to +15551234567 (refused if sending is turned on)"
if ! SENT=$(curl -fsS -X POST "$API_URL/fax" -H "X-API-Key: $TOKEN" -F to=+15551234567 -F queue_only=true \
    -F "file=@$DOC;type=text/plain"); then
  echo "[x] Faxbot refused the fax. Turn sending off first (System → Setup), or use scripts/send-fax.sh to really send." >&2
  exit 1
fi
JOB_ID=$(echo "$SENT" | json_field id)
echo "[i] Fax $JOB_ID: $(echo "$SENT" | json_field delivery_state)"

echo "[i] Reading its status with the same key"
curl -fsS "$API_URL/fax/$JOB_ID" -H "X-API-Key: $TOKEN" | sed -e 's/^/[status] /'
echo
echo "[✓] Done"
