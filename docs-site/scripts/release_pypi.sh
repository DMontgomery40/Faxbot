#!/usr/bin/env bash
set -euo pipefail

# Publishes Python packages to PyPI. Requires TWINE_USERNAME/TWINE_PASSWORD or a token in ~/.pypirc.

ROOT_DIR=$(cd "$(dirname "$0")/.." && pwd)

release_pkg() {
  local dir="$1"
  echo "Building $dir"
  pushd "$dir" >/dev/null
  python3 -m pip install --upgrade build twine >/dev/null
  python3 -m build
  python3 -m twine upload dist/*
  popd >/dev/null
}

release_pkg "$ROOT_DIR/sdks/python"
release_pkg "$ROOT_DIR/python_mcp"

echo "Done."

