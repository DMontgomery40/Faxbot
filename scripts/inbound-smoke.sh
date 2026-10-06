#!/usr/bin/env bash
# Check that received faxes reach Faxbot's public API, without a call:
# add a test fax (a real one-page PDF, marked as a test everywhere), list it
# with a read-only key and download its PDF. The read-only key is revoked on exit.
#
#   API_KEY=<admin key> FAX_API_URL=http://localhost:8080 scripts/inbound-smoke.sh
#
# The test fax goes through owners, mailbox rules and email delivery like a real one.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
source "$ROOT_DIR/scripts/load-env.sh"

API_URL="${FAX_API_URL:-http://localhost:8080}"
ADMIN_KEY="${API_KEY:-}"
OUT_DIR="${OUT_DIR:-${TMPDIR:-/tmp}}"

json_field() { python3 -c 'import json,sys; print(json.load(sys.stdin).get(sys.argv[1]) or "")' "$1"; }

[[ -n "$ADMIN_KEY" ]] || { echo "[x] Set API_KEY to an admin key (System → Access → Keys)." >&2; exit 1; }
curl -fsS "$API_URL/health" >/dev/null || { echo "[x] Faxbot does not answer at $API_URL" >&2; exit 1; }

echo "[i] Creating a read-only key for received faxes"
CREATED=$(curl -fsS -X POST "$API_URL/admin/api-keys" -H "X-API-Key: $ADMIN_KEY" -H 'Content-Type: application/json' \
  -d '{"name":"inbound-smoke","owner":"scripts","scopes":["inbound:list","inbound:read","inbound:document"]}')
KEY_ID=$(echo "$CREATED" | json_field key_id)
TOKEN=$(echo "$CREATED" | json_field token)
[[ -n "$TOKEN" ]] || { echo "[x] Faxbot did not create the key." >&2; exit 1; }
trap 'curl -fsS -X DELETE "$API_URL/admin/api-keys/$KEY_ID" -H "X-API-Key: $ADMIN_KEY" >/dev/null && echo "[i] Read-only key revoked"' EXIT

echo "[i] Adding a test fax"
# No fax number: a fax on a number is visible only to keys granted that number.
ID=$(curl -fsS -X POST "$API_URL/admin/inbound/simulate" -H "X-API-Key: $ADMIN_KEY" -H 'Content-Type: application/json' \
  -d '{}' | json_field id)
[[ -n "$ID" ]] || { echo "[x] The test fax was not added (is receiving turned on?)." >&2; exit 1; }
echo "[i] Test fax $ID"

curl -fsS "$API_URL/inbound/$ID" -H "X-API-Key: $TOKEN" >/dev/null || { echo "[x] The read-only key cannot read the fax." >&2; exit 1; }
FILE="$OUT_DIR/faxbot-inbound-$ID.pdf"
curl -fsS "$API_URL/inbound/$ID/pdf" -H "X-API-Key: $TOKEN" -o "$FILE"
if [[ "$(head -c 5 "$FILE")" != "%PDF-" ]]; then
  echo "[x] The downloaded file is not a PDF: $FILE" >&2
  exit 1
fi
echo "[✓] Received fax listed and its PDF saved: $FILE"
