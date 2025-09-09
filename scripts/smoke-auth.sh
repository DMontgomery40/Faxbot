#!/usr/bin/env bash
set -euo pipefail

# Simple smoke test for multi-key auth using FastAPI TestClient (no server needed)
# - Creates venv if missing
# - Installs API requirements
# - Runs only the auth smoke test

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
VENV_DIR="$ROOT_DIR/.venv"

if [[ ! -x "$VENV_DIR/bin/python" ]]; then
  python3 -m venv "$VENV_DIR"
fi

"$VENV_DIR/bin/python" -m pip install -q --upgrade pip >/dev/null 2>&1 || true
"$VENV_DIR/bin/python" -m pip install -q -r "$ROOT_DIR/api/requirements.txt"

echo "Running auth smoke test (pytest api/tests/test_api_keys.py)"
"$VENV_DIR/bin/pytest" -q api/tests/test_api_keys.py

echo "OK: auth smoke test passed"

