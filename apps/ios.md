---
layout: default
title: iOS (Faxbot Companion)
parent: Apps
nav_order: 1
permalink: /apps/ios/
---

# Faxbot Companion (iOS)

Lightweight, secure iOS companion that sends faxes through your Faxbot API. The app never communicates with providers directly; it calls your Faxbot server’s REST endpoints.

## Highlights
- iOS 26 support; Xcode 26 toolchain.
- Confirmation step before sending files (photo/scan/PDF).
- Photos and scans combine into a single PDF on send.
- Contacts picker filters for fax numbers (labels containing “fax”).
- Dark/light icons via Icon Composer; automatically sized.

## Availability
- Distributed by Faxbot. For access, contact support.
  - Managed distribution options available on request.

## UX Flow (Send)
- Enter a recipient:
  - Type a number (auto‑formatted); tap the checkmark to add, or
  - Tap “+” to open the device contacts (fax‑only) picker.
- Choose an attachment:
  - Pick Photo → confirmation appears; multiple images are combined to PDF.
  - Scan Document → uses VisionKit; pages are combined to PDF.
  - Upload PDF → confirms filename and size.
- Confirm dialog shows: “Send this {photo(s)|scan|PDF} to {name|number}?” with Send/Cancel.

Notes
- Max raw file size enforced by server (default 10 MB); images are converted to PDF before upload.
- The app never stores PHI in logs; device permissions are requested only when needed.

## Home Screen Name
- On‑device label is “Faxbot” (CFBundleDisplayName).
- App Store listing name can differ (e.g., “Faxbot Companion”).

## Permissions
- Contacts (NSContactsUsageDescription): “Allow access to find contacts with fax numbers.”
- Photos (NSPhotoLibraryUsageDescription/…AddUsageDescription)
- Camera (NSCameraUsageDescription) — for document scanning and QR pairing.

## Setup
- Pairing: From the Admin Console, generate a pairing code (or QR). In the app, open Settings → Redeem Code (or Scan QR) to connect to your Faxbot server.

## Troubleshooting
- “Attempted to access privacy sensitive data without a usage description” → Ensure NSContactsUsageDescription appears in the built Info.plist. XcodeGen injects this from `ios/FaxbotApp/project.yml`.
- “Unassigned children” warnings in AppIcon → ensure `AppIconV2.appiconset/Contents.json` references only the icons you’ve included; remove stray files.
- Constraint warnings from keyboard accessory bars → the app doesn’t rely on a custom accessory bar; if you add one, avoid conflicting bottom constraints with the system keyboard.

## Security & PHI
- The app never logs attachments or fax numbers.
- All network calls go to your Faxbot server; no provider SDKs are included.
- Respect server‑side HIPAA settings and rate limits.
