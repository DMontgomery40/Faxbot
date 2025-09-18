#!/usr/bin/env bash
set -euo pipefail

SCHEME=${SCHEME:-Faxbot}
DEVICE_NAME=${DEVICE_NAME:-"iPhone 17 Pro"}

ROOT_DIR="$(cd "$(dirname "$0")/../.." && pwd)"
APP_DIR="$ROOT_DIR/ios/FaxbotApp"

cd "$APP_DIR"

which xcodegen >/dev/null || { echo "Install xcodegen (brew install xcodegen)" >&2; exit 1; }

xcodegen generate

DEST="platform=iOS Simulator,name=${DEVICE_NAME}"
echo "[+] Building $SCHEME for $DEVICE_NAME"
# Force a local derived data path so the .app path is predictable
xcodebuild -scheme "$SCHEME" -configuration Debug -destination "$DEST" -derivedDataPath build build | xcpretty || true

APP_PATH="$PWD/build/Build/Products/Debug-iphonesimulator/Faxbot.app"
if [ ! -d "$APP_PATH" ]; then
  # Try to locate the built app if the default path changes
  APP_PATH=$(find "$PWD/build" -type d -name "Faxbot.app" -maxdepth 6 | head -n 1 || true)
fi

# Extract simulator UDID robustly (handles parentheses and state labels)
UDID=$(xcrun simctl list devices | grep -F "$DEVICE_NAME (" | head -n1 | sed -E 's/.*\(([0-9A-Fa-f-]{36})\).*/\1/')
if [ -z "$UDID" ]; then echo "Simulator $DEVICE_NAME not found" >&2; exit 1; fi

echo "[+] Booting simulator $DEVICE_NAME ($UDID)"
xcrun simctl boot "$UDID" || true
sleep 2

echo "[+] Installing app"
xcrun simctl install "$UDID" "$APP_PATH"

echo "[+] Launching app"
xcrun simctl launch "$UDID" net.faxbot.ios || true

