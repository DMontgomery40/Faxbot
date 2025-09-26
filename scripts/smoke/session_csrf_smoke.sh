#!/usr/bin/env bash
set -euo pipefail

# Dev smoke for cookie sessions + CSRF. Requires:
#  - FAXBOT_SESSIONS_ENABLED=true
#  - FAXBOT_CSRF_ENABLED=true
#  - FAXBOT_BOOTSTRAP_PASSWORD set (for admin login)
#
# Usage:
#   API=http://localhost:8080 USER=admin PASS=$FAXBOT_BOOTSTRAP_PASSWORD scripts/smoke/session_csrf_smoke.sh

API=${API:-http://localhost:8080}
USER=${USER:-admin}
PASS=${PASS:-${FAXBOT_BOOTSTRAP_PASSWORD:-}} # fallback for convenience

if [ -z "${PASS}" ]; then
  echo "PASS not set; export PASS or FAXBOT_BOOTSTRAP_PASSWORD" >&2
  exit 1
fi

TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

echo "[1/4] Login (cookie session)"
LOGIN_JSON=$(curl -sS -c "$TMP/cookies.txt" -H 'Content-Type: application/json' \
  -X POST "$API/auth/login" -d "{\"username\":\"$USER\",\"password\":\"$PASS\"}")
echo "$LOGIN_JSON" | jq . >/dev/null 2>&1 || true
CSRF=$(echo "$LOGIN_JSON" | jq -r '.csrf // empty' 2>/dev/null || true)
if [ -z "$CSRF" ]; then
  echo "[warn] CSRF token not returned (FAXBOT_CSRF_ENABLED may be false)." >&2
fi

echo "[2/4] Logout without CSRF (expect 403 when CSRF enabled)"
HTTP=$(curl -sS -o /dev/null -w '%{http_code}' -b "$TMP/cookies.txt" -X POST "$API/auth/logout" || true)
echo "HTTP $HTTP"

if [ -n "$CSRF" ]; then
  echo "[3/4] Logout with CSRF header (expect 200)"
  HTTP2=$(curl -sS -o /dev/null -w '%{http_code}' -b "$TMP/cookies.txt" -H "x-csrf-token: $CSRF" -X POST "$API/auth/logout" || true)
  echo "HTTP $HTTP2"
fi

echo "[4/4] Done"

