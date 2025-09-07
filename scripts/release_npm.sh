#!/usr/bin/env bash
set -euo pipefail

# Publishes Node packages to npm. Requires NPM_TOKEN to be configured (or prior `npm login`).

ROOT_DIR=$(cd "$(dirname "$0")/.." && pwd)

publish_pkg() {
  local dir="$1"
  echo "Publishing $dir"
  pushd "$dir" >/dev/null
  npm publish --access public
  popd >/dev/null
}

echo "Checking npm auth..."
npm whoami || echo "Warning: not logged in. Set NPM_TOKEN or run 'npm login' before publishing."

publish_pkg "$ROOT_DIR/node_mcp"
publish_pkg "$ROOT_DIR/sdks/node"

echo "Done."
