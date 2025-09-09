#!/usr/bin/env bash
# Load .env into current shell (exporting variables) if present.
set -euo pipefail
ENV_FILE="${ENV_FILE:-.env}"
if [[ -f "$ENV_FILE" ]]; then
  # shellcheck disable=SC2046
  set -a
  # Support CRLF line endings and comments
  sed -e 's/\r$//' "$ENV_FILE" > "$ENV_FILE.__tmp__"
  # shellcheck disable=SC1090
  source "$ENV_FILE.__tmp__"
  rm -f "$ENV_FILE.__tmp__"
  set +a
fi
