#!/usr/bin/env bash
# Wait for the next fax that arrives at Faxbot, then download its PDF: send a fax
# to one of your numbers while this runs. Shows the carrier trunk's status first
# when a trunk receives. Uses a read-only key that is revoked on exit.
#
#   API_KEY=<admin key> FAX_API_URL=http://localhost:8080 scripts/inbound-watch.sh
#   WAIT_MINUTES=10 (default) stops waiting after that long.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
source "$ROOT_DIR/scripts/load-env.sh"

API_URL="${FAX_API_URL:-http://localhost:8080}"
ADMIN_KEY="${API_KEY:-}"
WAIT_MINUTES="${WAIT_MINUTES:-10}"
OUT_DIR="${OUT_DIR:-${TMPDIR:-/tmp}}"

json_field() { python3 -c 'import json,sys; print(json.load(sys.stdin).get(sys.argv[1]) or "")' "$1"; }
newest_id() { python3 -c 'import json,sys; items=json.load(sys.stdin); print(items[0]["id"] if items else "")'; }

[[ -n "$ADMIN_KEY" ]] || { echo "[x] Set API_KEY to an admin key (System → Access → Keys)." >&2; exit 1; }
curl -fsS "$API_URL/health" >/dev/null || { echo "[x] Faxbot does not answer at $API_URL" >&2; exit 1; }

# The trunk's own sentences, when the trunk is set up (cloud providers have no trunk).
if TRUNK=$(curl -fsS "$API_URL/admin/sip/status" -H "X-API-Key: $ADMIN_KEY" 2>/dev/null) \
    && [[ "$(echo "$TRUNK" | json_field configured)" == "True" ]]; then
  echo "[i] Trunk: $(echo "$TRUNK" | json_field message)"
  echo "[i] $(echo "$TRUNK" | json_field registration_text)"
  HANDOVER=$(echo "$TRUNK" | json_field handover_text)
  [[ -z "$HANDOVER" ]] || echo "[i] $HANDOVER"
fi

CREATED=$(curl -fsS -X POST "$API_URL/admin/api-keys" -H "X-API-Key: $ADMIN_KEY" -H 'Content-Type: application/json' \
  -d '{"name":"inbound-watch","owner":"scripts","scopes":["inbound:list","inbound:read","inbound:document"]}')
KEY_ID=$(echo "$CREATED" | json_field key_id)
TOKEN=$(echo "$CREATED" | json_field token)
[[ -n "$TOKEN" ]] || { echo "[x] Faxbot did not create a read-only key." >&2; exit 1; }
trap 'curl -fsS -X DELETE "$API_URL/admin/api-keys/$KEY_ID" -H "X-API-Key: $ADMIN_KEY" >/dev/null && echo "[i] Read-only key revoked"' EXIT

BEFORE=$(curl -fsS "$API_URL/inbound?limit=1" -H "X-API-Key: $TOKEN" | newest_id)
echo "[i] Send a fax to one of your numbers now. Waiting up to $WAIT_MINUTES minutes (Ctrl+C stops)."
DEADLINE=$(( $(date +%s) + WAIT_MINUTES * 60 ))
while (( $(date +%s) < DEADLINE )); do
  sleep 5
  NEWEST=$(curl -fsS "$API_URL/inbound?limit=1" -H "X-API-Key: $TOKEN" | newest_id) || continue
  if [[ -n "$NEWEST" && "$NEWEST" != "$BEFORE" ]]; then
    FILE="$OUT_DIR/faxbot-inbound-$NEWEST.pdf"
    echo "[i] A fax arrived: $NEWEST"
    # The document can still be on its way from the provider; try for a minute.
    for _ in $(seq 1 12); do
      if curl -fsS "$API_URL/inbound/$NEWEST/pdf" -H "X-API-Key: $TOKEN" -o "$FILE" 2>/dev/null \
          && [[ "$(head -c 5 "$FILE")" == "%PDF-" ]]; then
        echo "[✓] Saved $FILE"
        exit 0
      fi
      sleep 5
    done
    echo "[x] The fax arrived but its document is not ready. Open it in Faxes → Received." >&2
    exit 1
  fi
done
echo "[x] No fax arrived within $WAIT_MINUTES minutes. Diagnostics (System → Diagnostics) shows what to check." >&2
exit 1
