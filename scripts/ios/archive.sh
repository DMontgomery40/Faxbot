#!/usr/bin/env bash
set -euo pipefail

APP_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
PRJ_DIR="$APP_ROOT/ios/FaxbotApp"

TEAM_ID=${DEVELOPMENT_TEAM:-}
if [ -z "$TEAM_ID" ]; then
  echo "Set DEVELOPMENT_TEAM=YOUR_TEAM_ID in env to sign." >&2
  exit 1
fi

cd "$PRJ_DIR"
xcodegen generate

SCHEME=Faxbot
ARCHIVE_PATH="$PWD/build/Faxbot.xcarchive"
EXPORT_PATH="$PWD/build/export"

echo "[+] Archiving"
xcodebuild -scheme "$SCHEME" -configuration Release \
  -destination 'generic/platform=iOS' \
  DEVELOPMENT_TEAM="$TEAM_ID" \
  archive -archivePath "$ARCHIVE_PATH"

echo "[+] Exporting .ipa for App Store (TestFlight)"
mkdir -p "$EXPORT_PATH"
cat > ExportOptions.plist <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>method</key>
  <string>app-store</string>
  <key>uploadBitcode</key>
  <false/>
  <key>compileBitcode</key>
  <false/>
  <key>signingStyle</key>
  <string>automatic</string>
  <key>destination</key>
  <string>export</string>
</dict>
</plist>
EOF

xcodebuild -exportArchive -archivePath "$ARCHIVE_PATH" -exportOptionsPlist ExportOptions.plist -exportPath "$EXPORT_PATH"

echo "[i] Exported to: $EXPORT_PATH"
echo "[i] Upload via Apple's Transporter or App Store Connect API."

