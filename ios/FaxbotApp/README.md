## Faxbot iOS (iOS 26)

CLI-first iOS app with iOS 26 Enhanced Security.

### Build (Simulator)

- Prereqs: Xcode, XcodeGen, xcpretty
- Run: `scripts/ios/build.sh`

### Archive & Export (TestFlight)

- Set env: `export DEVELOPMENT_TEAM=YOUR_TEAM_ID`
- Run: `scripts/ios/archive.sh`
- Upload with Transporter or Fastlane (below)

### Fastlane (optional)

- In `ios/FaxbotApp` run:
  - `bundle exec fastlane build` or `fastlane build`
  - `TEAM_ID=YOUR_TEAM_ID APP_IDENTIFIER=net.faxbot.ios.v1 fastlane beta`

### Enhanced Security (iOS 26)

- Entitlements enabled in `Resources/Faxbot.entitlements`:
  - hardened-process, enhanced-security-version=1, hardened-heap, platform-restrictions=2, dyld-ro
- Build settings enabled in `project.yml`:
  - ENABLE_POINTER_AUTHENTICATION, CLANG_ENABLE_*_TYPED_ALLOCATOR_SUPPORT, CLANG_ENABLE_STACK_ZERO_INIT,
    ENABLE_SECURITY_COMPILER_WARNINGS, ENABLE_C_BOUNDS_SAFETY, ENABLE_CPLUSPLUS_BOUNDS_SAFE_BUFFERS

### App Groups

- Enable `group.net.faxbot.shared` for both app and extension

### Config

- Pair via Admin Console → generate code → Settings → Redeem Code (or Scan QR)
- App stores Local/Tunnel URLs and token in Keychain + App Group for Share Extension

