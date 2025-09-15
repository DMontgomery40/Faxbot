---
layout: default
title: SignalWire Cloud Fax (Compatibility API)
parent: Backends
nav_order: 2
permalink: /backends/signalwire-setup.html
---

# SignalWire Cloud Fax (Compatibility API)

Who is this for
- Cloud‑first users who want a Phaxio‑like experience using SignalWire’s Compatibility (Twilio‑style) API.

What you’ll do
- Configure SignalWire credentials in Admin Console, ensure a public URL, send a test fax, and verify status callbacks.

Prerequisites
- SignalWire Space URL, Project ID, API Token
- A public HTTPS URL for your Faxbot API (`PUBLIC_API_URL`), or use a tunnel for testing

Steps
1) Open Admin Console → Settings
   - Backend: select `signalwire`
   - Fill: Space URL (e.g., `example.signalwire.com`), Project ID, API Token, From (fax)
   - Click “Apply & Reload”
2) Public URL for MediaUrl
   - Set `PUBLIC_API_URL` to an HTTPS URL reachable by SignalWire (see [Create a Public URL](/Faxbot/guides/public-url-tunnel/))
   - Faxbot generates a tokenized MediaUrl: `/fax/{jobId}/pdf?token=…`
3) Send a test fax
   - Admin Console → Send
   - Or via API: `POST /fax` with `to` and `file`
4) Status updates
   - SignalWire calls `/signalwire-callback` (configure StatusCallback if desired)
   - Admin Console → Jobs shows status; Diagnostics → Inbound Callbacks lists the callback URL

Security
- Use HTTPS for `PUBLIC_API_URL` in production
- Optionally set `SIGNALWIRE_WEBHOOK_SIGNING_KEY` to verify callback signatures

References
- SignalWire Fax Docs (Compatibility API): see [Third‑Party References](/Faxbot/third-party/)

