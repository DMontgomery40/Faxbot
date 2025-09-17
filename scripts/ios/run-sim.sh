#!/usr/bin/env bash
set -euo pipefail

SCHEME=${SCHEME:-Faxbot}
DEVICE_NAME=${DEVICE_NAME:-"iPhone 15 Pro"}

ROOT_DIR="$(cd "$(dirname "$0")/../.." && pwd)"
APP_DIR="$ROOT_DIR/ios/FaxbotApp"

cd "$APP_DIR"

which xcodegen >/dev/null || { echo "Install xcodegen (brew install xcodegen)" >&2; exit 1; }

xcodegen generate

DEST="platform=iOS Simulator,name=${DEVICE_NAME}"
echo "[+] Building $SCHEME for $DEVICE_NAME"
xcodebuild -scheme "$SCHEME" -configuration Debug -destination "$DEST" build | xcpretty || true

APP_PATH="$PWD/build/Build/Products/Debug-iphonesimulator/Faxbot.app"
if [ ! -d "$APP_PATH" ]; then
  # Fallback default derived data path
  APP_PATH="$(getconf DARWIN_USER_TEMP_DIR || echo /tmp)/Faxbot.app"
fi

UDID=$(xcrun simctl list devices | awk -v n="$DEVICE_NAME" '$0 ~ n" \("{gsub(/[()]/,""); print $2; exit}')
if [ -z "$UDID" ]; then echo "Simulator $DEVICE_NAME not found" >&2; exit 1; fi

echo "[+] Booting simulator $DEVICE_NAME ($UDID)"
xcrun simctl boot "$UDID" || true
sleep 2

echo "[+] Installing app"
xcrun simctl install "$UDID" "$APP_PATH"

echo "[+] Launching app"
xcrun simctl launch "$UDID" net.faxbot.ios || true

