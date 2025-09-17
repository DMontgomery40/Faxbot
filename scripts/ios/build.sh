#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/../.." && pwd)"
APP_DIR="$ROOT_DIR/ios/FaxbotApp"

cd "$APP_DIR"

if ! command -v xcodegen >/dev/null 2>&1; then
  echo "[hint] Install XcodeGen: brew install xcodegen" >&2
fi

echo "[+] Generating Xcode project via XcodeGen"
xcodegen generate

SCHEME="Faxbot"
DEST_SIM="platform=iOS Simulator,name=iPhone 15 Pro"

echo "[+] Building for iOS Simulator"
xcodebuild -scheme "$SCHEME" -destination "$DEST_SIM" -configuration Debug build | xcpretty || true

echo "[i] To build for devices: supply a development team and run:\n    xcodebuild -scheme $SCHEME -destination 'generic/platform=iOS' DEVELOPMENT_TEAM=YOURTEAMID build"

