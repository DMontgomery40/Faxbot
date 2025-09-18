TL;DR — What iOS 26 changes matter for Faxbot

SDK & App Store: Apple now expects/accepts apps built with Xcode 26 / iOS 26 SDK for TestFlight/App Store submissions. If you want to upload builds compiled against iOS 26 SDK, you must use Xcode 26 (or an Xcode 26 beta supported by App Store Connect). Plan to test and compile with Xcode 26 during beta. 
Apple Developer

WebKit/WKWebView: Safari/WebKit got a major 26.0 update (many engine improvements and behavior tweaks). That can change rendering/JS performance in any WKWebView that embeds the Admin Console; test the Admin Console in WKWebView on iOS 26 early. Don’t assume parity with desktop Safari — test both. 
WebKit

Privacy / Local network: Local-network permission rules remain (NSLocalNetworkUsageDescription still required for LAN discovery / local IP access). If users connect via LAN IPs or .local, iOS 26 will prompt for local network access — include an explicit rationale string. 
Apple Developer

Transport & TLS: ATS is still enforced — TLS1.2+ (prefer TLS1.3) required, and Apple is deprecating legacy TLS. If you support self-signed certs you must handle pinning/TOFU and document that ATS exceptions are narrow. 
Apple Developer
+1

Platform-wide UX changes (Liquid Glass, Photos/Preview): iOS 26 introduces system UI redesign and improvements to Photos/Preview. Document scanning + PDF viewing still uses VisionKit/PDFKit/QuickLook but some rendering / photo picker UX may feel different on devices running iOS 26. Test scanning/preview flows on iOS 26 devices. 
Apple
+1

Concrete iOS-26 Impact Summary (short)

Build with Xcode 26 to target iOS 26 SDK and use iOS-26 specific fixes/behaviour. App Store Connect already accepts Xcode 26 CI uploads. 
Apple Developer

WKWebView: test Admin Console pages (JS, cookies, CORS, storage, service workers, WebAuthn / passkeys) — WebKit 26 includes many net & JS changes; some web features may behave slightly differently in embedded WKWebView vs desktop. 
WebKit

Local network: If users will use LAN addresses, add NSLocalNetworkUsageDescription and test the permission UX flow and fallback messaging. 
Apple Developer

TLS/ATS: keep TLS 1.2+ and prefer TLS1.3; implement certificate fingerprint pinning / TOFU for self-signed servers. Apple’s “upcoming requirements” warn against legacy TLS. 
Apple Developer
+1

Photo / Preview changes: scanning and PDF preview still supported; test VisionKit + PDFKit on iOS 26 devices to confirm file sizes, compression defaults, and PDF rendering quality. 
Apple

Updated, iOS-26-aware Implementation Plan (explicit steps — one dev, new to Swift)

Below is a step-by-step doable plan. I include concrete commands for building and uploading (Fastlane + xcodebuild) — copy/paste ready. I also list every Info.plist key and entitlement you’ll need for TestFlight and App Store.

A — Environment + dev setup (do this first)

Install Xcode 26 (use App Store or download beta from developer.apple.com if needed). Use macOS version compatible with Xcode 26.

After installing, open Xcode and sign in with your Apple ID (Xcode → Settings → Accounts).

Register App IDs in Apple Developer:

Create App ID (Bundle ID): e.g. com.faxbot.app

Create an App Group ID for sharing with the Share Extension: e.g. group.com.faxbot.app

Enable Capabilities in your Xcode project (Signing & Capabilities):

App Groups (use the exact group you created)

Keychain Access Groups / Keychain Sharing (if you want extension -> keychain sharing; App Group is preferred)

Camera usage, Photo library, Local network (via Info.plist)

(No push/APNs yet for v1 — skip)

Install Fastlane (I recommend Homebrew for speed):

echo "brew update && brew install fastlane" && echo "fastlane --version && which fastlane"


(If brew isn't installed on the dev machine, install it first — but most mac dev boxes already have brew.)

B — Project skeleton & Info.plist keys (exact keys to add)

Add these Info.plist keys with user-facing strings:

NSCameraUsageDescription — "Allow Faxbot to use the camera to scan documents for faxing."

NSPhotoLibraryAddUsageDescription — "Allow Faxbot to save scanned PDFs if you choose to save them."

NSPhotoLibraryUsageDescription — "Allow Faxbot to pick images/documents from your Photos if you want to fax them."

NSLocalNetworkUsageDescription — "Allow Faxbot to discover or connect to a Faxbot server on your local network (required for LAN servers)."

NSAppTransportSecurity — do not add a global NSAllowsArbitraryLoads. If you must support a specific self-signed domain for testing, add a narrow exception per-domain (but prefer pinning). See ATS docs. 
Apple Developer
+1

UIFileSharingEnabled — true (optional; not required unless you want iTunes file sharing)

LSSupportsOpeningDocumentsInPlace — true (for file handling)

CFBundleLocalizations — include locales you want.

Entitlements / Capabilities:

App Group: group.com.faxbot.app

Keychain Access Groups: default is fine; we'll store only API tokens in Keychain.

Background modes: none for v1.

C — Networking & TLS (explicit approach)

Use URLSession with async/await wrappers.

Certificate pinning / TOFU approach for self-signed servers:

On pairing (QR or manual), fetch the server certificate fingerprint (SHA256) from the server (endpoint GET /.well-known/faxbot-cert or the pairing token flow returns it).

Store fingerprint in Keychain.

For each request, implement URLSession delegate urlSession(_:didReceive:completionHandler:) to verify the cert's SHA256 fingerprint matches the stored one. If it matches, allow; otherwise, block and show an explicit error.

Add a narrow ATS exception only if absolutely necessary for connections that otherwise fail due to untrusted certs; still require TLS. (Prefer pinning + ATS exception for that specific host rather than global exceptions.) See Apple docs on ATS. 
Apple Developer
+1

D — WKWebView (Admin Console wrapper) — iOS 26 notes

Use WKWebView + WKWebsiteDataStore.default() to load mobile Admin Console pages.

Test for storage and cookie behavior on iOS 26 — WebKit 26 changed internal behaviors; ensure login cookies persist across sessions in WKWebView (if admin console relies on cookies). If login is problematic, prefer an explicit API-based session using scoped mobile API keys instead of relying on WKWebView cookie auth. 
WebKit

E — VisionKit scanner, PHPicker, PDFKit — iOS 26 notes

Use VNDocumentCameraViewController (VisionKit) for multi-page scanning. It still exists and is stable. After scanning, convert pages to a single PDF using PDFKit or CoreGraphics. Test the default scan settings under iOS 26 to ensure resulting PDF sizes are acceptable; adjust compression to keep under the server max_upload_bytes. 
Apple

Use PHPickerViewController for picking photos without full Photo Library permission. It’s still preferable to legacy UIImagePickerController. (No iOS 26 breaking changes here.)

F — Share Extension specifics (memory constraints)

Create a Share Extension target. Add the same App Group to the extension so it can read server URL / API key from the shared container (or use Keychain access group).

Important: Share extensions have tight memory limits. Never load full-res images into memory inside the extension. Use file URLs and streaming uploads via URLSessionUploadTask from disk. If you must process images, downsample first. Many devs crash Share Extension by loading a huge UIImage. (This is an old issue still relevant in iOS 26.)

Include the extension in the App Store submission and TestFlight build.

G — TestFlight & App Store Connect (iOS 26 specifics)

Build with Xcode 26. Upload via Xcode Organizer or Fastlane.

App Store Connect accepts Xcode 26 builds; App Store Connect release notes mention support for “Enhanced Security capability” in iOS 26 builds — you likely don’t need that capability for v1, but be aware. 
Apple Developer

For external TestFlight: include demo test server creds in the TestFlight notes (so Apple reviewers can test the app). Provide a short test plan in the metadata explaining how to pair and test scanning/sending.

For external reviewers, make it easy: provide a test server URL and a mobile pairing QR or API key so the reviewer can exercise the main flows without owning their own server.

Copy-paste friendly commands & sample Fastlane lane

Below are quick commands you can run to build & upload a TestFlight beta using fastlane. Replace environment variables as needed — I list the vars you should export first so the script runs without interactive prompts.

Set env vars (one time per shell)
(You will need to create an App Store Connect API key in App Store Connect and set its file path; Fastlane is flexible, but below I use FASTLANE_APPLE_APPLICATION_SPECIFIC_PASSWORD approach for simplicity in small setups.)

echo "export APP_IDENTIFIER=com.faxbot.app && export APPLE_ID=your.apple.id@example.com && export TEAM_ID=YOURTEAMID && export FASTLANE_SESSION=PLACE_YOUR_FASTLANE_SESSION_HERE" && \
echo "Environment variables set. Update APPLE_ID, TEAM_ID, and FASTLANE_SESSION as appropriate."


Install fastlane and dependencies (macOS)

echo "brew update && brew install fastlane && fastlane --version"


Sample Fastlane Fastfile lane — place this in fastlane/Fastfile (this is a complete lane you can paste into a Fastfile):

default_platform(:ios)

platform :ios do
  desc "Build and upload to TestFlight (iOS 26 SDK)"
  lane :beta do
    build_app(
      scheme: "Faxbot",
      workspace: "Faxbot.xcworkspace",
      export_method: "app-store",
      export_options: {
        provisioningProfiles: {
          "com.faxbot.app" => "match AppStore com.faxbot.app"
        }
      },
      clean: true,
      silent: false
    )

    upload_to_testflight(
      skip_waiting_for_build_processing: false,
      team_id: ENV["TEAM_ID"]
    )
  end
end


Run the lane (from your project root):

echo "fastlane beta"


If you prefer plain xcodebuild + altool (more manual), here’s a minimal archive + upload example (you still need code signing set up in Xcode):

echo "xcodebuild -workspace Faxbot.xcworkspace -scheme Faxbot -configuration Release -archivePath /tmp/Faxbot.xcarchive archive" && \
echo "xcodebuild -exportArchive -archivePath /tmp/Faxbot.xcarchive -exportOptionsPlist ExportOptions.plist -exportPath /tmp/FaxbotExport" && \
echo "xcrun altool --upload-app -f /tmp/FaxbotExport/Faxbot.ipa -u your.apple.id@example.com -p APP_SPECIFIC_PASSWORD"


NOTE: altool is being phased out for some actions; Fastlane wraps the modern upload flow and App Store Connect API. For iOS 26 SDK uploads it’s simplest to use Fastlane upload_to_testflight or Xcode Organizer.

Testing matrix (must do on real hardware with iOS 26)

VisionKit scanning: test single page, multi-page, different lighting, grayscale vs color, resulting PDF size.

WKWebView Admin Console: login persistence, local storage, cookie handling, CORS, file upload from the console (if used).

Local network pairing: test QR + LAN IP flow; ensure NSLocalNetworkUsageDescription prompt text is clear.

Self-signed cert pinning: verify pairing and cert rollover (store two pins before expiry).

Share Extension: share PDFs from Files, images from Photos, try large images to validate memory behavior.

TestFlight external review: provide test server & credentials in the TestFlight notes.

App Review & Privacy checklist for submission (iOS 26)

Provide clear privacy policy URL (App Store requires it). State that the app is a thin client that talks to the user’s self-hosted server; the app does not send data to third parties.

Fill out the App Privacy form in App Store Connect honestly (what data the app sends/receives). For v1: likely “Identifiers (device ID), Usage data (only for diagnostics if you enable it) — but default to no analytics.*”

For external TestFlight review: include a test account or pairing token & step-by-step instructions in the review notes so Apple can test the app’s core functionality. 
Apple Developer

iOS 26-specific gotchas & risks (short list)

WebKit behavior: subtle rendering and storage changes in WebKit 26 could break complex Admin Console JS — test early. 
WebKit

New OS visuals: UI might look subtly different with Liquid Glass; verify contrast and legibility for scan preview screens. 
The Verge

TLS/ATS enforcement: if your users run old TLS stacks on their servers, they may be blocked by default — communicate TLS requirements to users. 
Apple Developer

Final explicit checklist (copy & do)

Install Xcode 26 on dev Mac.

Create App ID + App Group in Apple Developer.

Start Xcode project (Swift + SwiftUI primary). Add Share Extension target.

Add Info.plist keys listed earlier.

Implement pairing endpoint in server: POST /mobile/pair that returns mobile-scoped API key + server cert fingerprint. (I can write the exact API spec next.)

Implement VisionKit scanning flow -> convert images -> create PDF -> compress (goal < 10 MB). Test on iOS 26 device.

Implement networking with URLSession; add cert pinning delegate for self-signed servers.

Implement WKWebView wrapper for Admin Console; test all pages on iOS 26.

Implement Share Extension using App Group; test memory/streamed upload on iOS 26.

Build + upload via Fastlane lane fastlane beta. Provide TestFlight notes with demo server credentials.

Test on multiple iOS 26 devices (and one iOS 17–25 device for backwards compat).

David — I fixed the SDK target and updated the plan for iOS 26. Want me to:

(A) Draft the exact POST /mobile/pair spec (request/response JSON, token lifetime, fingerprint format) so your backend can return the mobile-scoped key and cert fingerprint?

(B) Write the Swift code (full, copy-pasteable) for: the VisionKit scanner → PDF creation pipeline (including compression), and the URLSession cert-pinning delegate (so you can copy/paste and run)?

Say A, B, or both and I’ll drop the complete code + server spec next.