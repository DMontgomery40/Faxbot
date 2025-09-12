#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
source "$ROOT_DIR/scripts/load-env.sh"

ENV_FILE="${ENV_FILE:-$ROOT_DIR/.env}"

errors=0
warnings=0

function title() { echo; echo "== $1 =="; }
function ok() { printf "[OK] %s\n" "$1"; }
function warn() { printf "[WARN] %s\n" "$1"; warnings=$((warnings+1)); }
function fail() { printf "[FAIL] %s\n" "$1"; errors=$((errors+1)); }

istrue() {
  local v
  v=$(printf '%s' "${1:-}" | tr '[:upper:]' '[:lower:]')
  [[ "$v" == "1" || "$v" == "true" || "$v" == "yes" ]]
}

val() {
  local name="$1"
  printf '%s' "${!name-}"
}

require() {
  local name="$1" desc="$2"
  local v
  v=$(val "$name")
  if [[ -z "$v" ]]; then
    fail "$name is required: $desc"
  else
    ok "$name present"
  fi
}

require_not() {
  local name="$1" bad="$2" desc="$3"
  local v
  v=$(val "$name")
  if [[ "$v" == "$bad" ]]; then
    fail "$name must not be '$bad' ($desc)"
  fi
}

title "Environment overview"
BACKEND="${FAX_BACKEND:-}"
if [[ -z "$BACKEND" ]]; then
  fail "FAX_BACKEND is not set (expected: phaxio | sinch | sip)"
else
  ok "FAX_BACKEND=$BACKEND"
fi

INBOUND="${INBOUND_ENABLED:-false}"
ok "INBOUND_ENABLED=${INBOUND}"

STORAGE="${STORAGE_BACKEND:-local}"
ok "STORAGE_BACKEND=${STORAGE}"

title "Base settings"
require MAX_FILE_SIZE_MB "Max upload size in MB (default 10)"
if istrue "${REQUIRE_API_KEY:-false}"; then
  ok "REQUIRE_API_KEY=true (recommended)"
else
  warn "REQUIRE_API_KEY=false (dev convenience; not recommended for production)"
fi

case "$(printf '%s' "${BACKEND}" | tr '[:upper:]' '[:lower:]')" in
  phaxio)
    title "Phaxio backend"
    require PHAXIO_API_KEY "Phaxio API key from console"
    require PHAXIO_API_SECRET "Phaxio API secret from console"
    require PUBLIC_API_URL "Public URL used by Phaxio to fetch PDFs"
    if istrue "${ENFORCE_PUBLIC_HTTPS:-false}"; then
      if [[ "${PUBLIC_API_URL:-}" =~ ^http:// ]] && [[ ! "${PUBLIC_API_URL:-}" =~ localhost|127\.0\.0\.1 ]]; then
        fail "PUBLIC_API_URL must be HTTPS when ENFORCE_PUBLIC_HTTPS=true"
      else
        ok "PUBLIC_API_URL scheme ok"
      fi
    else
      warn "ENFORCE_PUBLIC_HTTPS=false (ok for dev; set true for production)"
    fi
    ;;
  sinch)
    title "Sinch Fax API v3 backend"
    require SINCH_PROJECT_ID "Sinch project ID"
    if [[ -z "${SINCH_API_KEY:-}" || -z "${SINCH_API_SECRET:-}" ]]; then
      if [[ -n "${PHAXIO_API_KEY:-}" && -n "${PHAXIO_API_SECRET:-}" ]]; then
        warn "SINCH_API_* not set; will fall back to PHAXIO_API_* (set explicit SINCH_API_* in production)"
      else
        fail "SINCH_API_KEY/SINCH_API_SECRET required (or provide PHAXIO_* fallback)"
      fi
    else
      ok "SINCH_API_KEY/SINCH_API_SECRET present"
    fi
    ;;
  sip)
    title "SIP/Asterisk backend"
    require ASTERISK_AMI_HOST "Asterisk AMI host (internal/private)"
    require ASTERISK_AMI_PORT "Asterisk AMI port (default 5038)"
    require ASTERISK_AMI_USERNAME "AMI username"
    require ASTERISK_AMI_PASSWORD "AMI password"
    require_not ASTERISK_AMI_PASSWORD "changeme" "set a strong password"
    require SIP_USERNAME "SIP trunk username"
    require SIP_PASSWORD "SIP trunk password"
    require SIP_SERVER "Provider SIP server/realm"
    require SIP_FROM_USER "Caller ID / DID in E.164 (+1555...)"
    if [[ -z "${SIP_FROM_DOMAIN:-}" ]]; then
      warn "SIP_FROM_DOMAIN not set (often same as SIP_SERVER)"
    fi
    ;;
  *)
    if [[ -n "$BACKEND" ]]; then warn "Unknown FAX_BACKEND=$BACKEND"; fi
    ;;
esac

if istrue "$INBOUND"; then
  title "Inbound (enabled)"
  case "$(printf '%s' "${BACKEND}" | tr '[:upper:]' '[:lower:]')" in
    sip)
      require ASTERISK_INBOUND_SECRET "Secret for internal Asterisk→API posts"
      ;;
  esac
  # Cloud inbound verification (optional but recommended)
  if istrue "${PHAXIO_INBOUND_VERIFY_SIGNATURE:-true}"; then ok "Phaxio inbound signature verification enabled"; else warn "PHAXIO_INBOUND_VERIFY_SIGNATURE=false"; fi
  if [[ -n "${SINCH_INBOUND_BASIC_USER:-}" || -n "${SINCH_INBOUND_HMAC_SECRET:-}" ]]; then
    ok "Sinch inbound verification configured (Basic/HMAC)"
  else
    warn "Sinch inbound verification not configured (set SINCH_INBOUND_BASIC_USER/PASS and/or SINCH_INBOUND_HMAC_SECRET)"
  fi
else
  title "Inbound (disabled)"
  ok "INBOUND_ENABLED=false"
fi

title "Storage"
case "$(printf '%s' "${STORAGE}" | tr '[:upper:]' '[:lower:]')" in
  s3)
    require S3_BUCKET "Target S3 bucket for inbound PDFs"
    if [[ -z "${S3_REGION:-}" ]]; then warn "S3_REGION not set"; else ok "S3_REGION set"; fi
    if [[ -z "${S3_KMS_KEY_ID:-}" ]]; then warn "S3_KMS_KEY_ID not set (SSE‑KMS recommended for HIPAA)"; else ok "S3_KMS_KEY_ID set (SSE‑KMS)"; fi
    if [[ -n "${S3_ENDPOINT_URL:-}" ]]; then ok "S3 endpoint set (S3‑compatible: $S3_ENDPOINT_URL)"; fi
    ;;
  local|*)
    ok "Using local storage (dev only). Configure S3 for production."
    ;;
esac

echo
echo "Summary: $errors error(s), $warnings warning(s)."
if (( errors > 0 )); then exit 1; fi
