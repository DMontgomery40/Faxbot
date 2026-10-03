#!/usr/bin/env bash
set -euo pipefail

# Usage: scripts/get-status.sh <job_id>

JOB=${1:-}
if [[ -z "${JOB}" ]]; then
  echo "Usage: $0 <job_id>" >&2
  exit 1
fi

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"

# Every request needs a Faxbot API key. Use FAXBOT_API_KEY when it is set,
# otherwise API_KEY from .env (read without executing the file).
API_URL=${FAX_API_URL:-http://localhost:8080}
KEY=${FAXBOT_API_KEY:-}
if [[ -f "${ROOT_DIR}/.env" ]]; then
  while IFS='=' read -r k v; do
    case "$k" in
      'FAX_API_URL') API_URL=${v};;
      'API_KEY') if [[ -z "${KEY}" ]]; then KEY=${v}; fi ;;
    esac
  done < <(grep -E '^(FAX_API_URL|API_KEY)=' "${ROOT_DIR}/.env" || true)
fi
if [[ -z "${KEY}" ]]; then
  echo "Set FAXBOT_API_KEY to a Faxbot API key (or API_KEY in .env). Every request needs one." >&2
  exit 1
fi
API_KEY_HEADER=(-H "X-API-Key: ${KEY}")

echo "GET ${API_URL}/fax/${JOB}" >&2
curl -sS "${API_URL}/fax/${JOB}" "${API_KEY_HEADER[@]}" | jq .
