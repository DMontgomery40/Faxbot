#!/usr/bin/env bash
set -euo pipefail

# Start the Faxbot API locally with uvicorn using the current working tree.
# Useful when you want the latest endpoints (e.g., /admin/api-keys) without Docker.

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
VENV_DIR="$ROOT_DIR/.venv"

if [[ ! -x "$VENV_DIR/bin/python" ]]; then
  python3 -m venv "$VENV_DIR"
fi

"$VENV_DIR/bin/python" -m pip install -q --upgrade pip >/dev/null 2>&1 || true
"$VENV_DIR/bin/python" -m pip install -q -r "$ROOT_DIR/api/requirements.txt"

# Default dev env (override by exporting before running)
export API_KEY="${API_KEY:-bootstrap_admin_only}"
export REQUIRE_API_KEY="${REQUIRE_API_KEY:-true}"
export FAX_DISABLED="${FAX_DISABLED:-true}"
export FAX_BACKEND="${FAX_BACKEND:-phaxio}"
export FAX_DATA_DIR="${FAX_DATA_DIR:-$ROOT_DIR/faxdata}"
export ENABLE_LOCAL_ADMIN="${ENABLE_LOCAL_ADMIN:-true}"

PORT="${PORT:-8080}"
echo "[i] Starting uvicorn on http://0.0.0.0:${PORT}"
echo "[i] API_KEY (bootstrap admin): $API_KEY"
echo "[i] REQUIRE_API_KEY: $REQUIRE_API_KEY"
echo "[i] FAX_DISABLED: $FAX_DISABLED"
echo "[i] FAX_BACKEND: $FAX_BACKEND"
echo "[i] FAX_DATA_DIR: $FAX_DATA_DIR"
echo "[i] ENABLE_LOCAL_ADMIN: $ENABLE_LOCAL_ADMIN"

exec "$VENV_DIR/bin/python" -m uvicorn api.app.main:app --host 0.0.0.0 --port "$PORT" --reload
