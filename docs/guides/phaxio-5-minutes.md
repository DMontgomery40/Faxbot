---
layout: default
title: Send Your First Fax (5‑Minute Phaxio)
parent: Guides
nav_order: 1
permalink: /guides/phaxio-5-minutes/
---

# Send Your First Fax (5‑Minute Phaxio)

Who is this for
- Operators and non‑technical users who want a quick, reliable send.

What you’ll do
- Configure Phaxio in the Admin Console, verify a public URL, and send a test fax.

Steps
1) Open Admin Console → Setup Wizard
   - Choose Backend: Phaxio (Recommended)
   - Enter API Key and Secret (from Phaxio console)
   - Set Callback URL to `<PUBLIC_API_URL>/phaxio-callback`
   - Pick Security Profile: HIPAA if handling PHI, else Non‑PHI
   - Click “Apply & Reload”
2) Public URL (if needed)
   - Use a tunnel during testing: [Create a Public URL](/Faxbot/guides/public-url-tunnel/)
   - Ensure `PUBLIC_API_URL` in Settings matches the tunnel URL (https)
3) Send a test fax
   - In Admin Console → Send, choose a PDF or TXT and enter a destination
   - Or via SDK: [Node](/Faxbot/sdks/node/), [Python](/Faxbot/sdks/python/)
4) Check status
   - Admin Console → Jobs shows queued/in‑progress/complete
   - API: `GET /fax/{id}`

Tips
- Allowed file types: PDF and TXT
- Max size: see `MAX_FILE_SIZE_MB` (default 10 MB)
- Enforce HTTPS and signature verification for production

Troubleshooting
- No status updates? Verify callback URL and `PUBLIC_API_URL` reachability
- “Invalid token” when provider fetches PDF: the per‑job token expired or was mismatched
- See [Troubleshooting](/Faxbot/troubleshooting/)

