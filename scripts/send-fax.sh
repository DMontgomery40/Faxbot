#!/usr/bin/env bash
set -euo pipefail

# Usage: scripts/send-fax.sh "+15551234567" /abs/path/file.{pdf,txt}

TO=${1:-}
FILE=${2:-}

if [[ -z "${TO}" || -z "${FILE}" ]]; then
  echo "Usage: $0 +15551234567 /path/to/file.pdf|.txt" >&2
  exit 1
fi

if [[ ! -f "${FILE}" ]]; then
  echo "File not found: ${FILE}" >&2
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

ct="application/pdf"
case "${FILE}" in
  *.txt) ct="text/plain";;
  *.pdf) ct="application/pdf";;
  *) echo "Unsupported file extension. Use .pdf or .txt" >&2; exit 1;;
esac

echo "POST ${API_URL}/fax" >&2
curl -sS -X POST "${API_URL}/fax" \
  "${API_KEY_HEADER[@]}" \
  -F "to=${TO}" \
  -F "file=@${FILE};type=${ct}" | jq .
