#!/usr/bin/env bash
set -euo pipefail

# Usage: scripts/get-status.sh <job_id>

JOB=${1:-}
if [[ -z "${JOB}" ]]; then
  echo "Usage: $0 <job_id>" >&2
  exit 1
fi

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"

# Load minimal keys from .env without executing it
API_URL=${FAX_API_URL:-http://localhost:8080}
API_KEY_HEADER=()
if [[ -f "${ROOT_DIR}/.env" ]]; then
  while IFS='=' read -r k v; do
    case "$k" in
      'FAX_API_URL') API_URL=${v};;
      'API_KEY') if [[ -n "$v" ]]; then API_KEY_HEADER=(-H "X-API-Key: ${v}"); fi ;;
    esac
  done < <(grep -E '^(FAX_API_URL|API_KEY)=' "${ROOT_DIR}/.env" || true)
fi

echo "GET ${API_URL}/fax/${JOB}" >&2
curl -sS "${API_URL}/fax/${JOB}" "${API_KEY_HEADER[@]}" | jq .
