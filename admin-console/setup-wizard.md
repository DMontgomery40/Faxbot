---
layout: default
title: Setup Wizard
parent: Admin Console
nav_order: 1
permalink: /admin-console/setup-wizard/
---

# Setup Wizard

Configure Faxbot end‑to‑end without touching `.env` files. The wizard validates inputs, applies safe defaults, and provides copy‑ready exports.

Steps
- Choose Backend
  - Phaxio (recommended): requires API key/secret and public HTTPS URL
  - Sinch Fax API v3: requires project ID, API key/secret
  - SIP/Asterisk: requires AMI host/port/username/password
- Security Profile
  - HIPAA (strict): require API key, enforce HTTPS, enable audit logging, verify signatures
  - Non‑PHI (convenience): relaxed HTTPS and logging defaults; still supports API keys
- Provider Details
  - Phaxio: set `PHAXIO_CALLBACK_URL` (usually `<PUBLIC_API_URL>/phaxio-callback`)
  - Sinch: set project region/base URL if needed
  - SIP: confirm Station ID and Header (sender metadata)
- Apply & Reload
  - Writes settings to the running process; shows status banner
  - “Generate .env” exports a file for persistence; you can also “Save .env to server” if persisted settings are enabled

Helpful tips
- Use a tunnel during initial Phaxio testing (cloudflared/ngrok). See: [Deployment](/Faxbot/deployment/) and `scripts/setup-phaxio-tunnel.sh`.
- If “Restart API” is available, changes that affect connections (e.g., AMI) will prompt a restart.
- The wizard never stores provider secrets in plugin config. Secrets live in environment variables.

Example .env snippets
- Phaxio (HIPAA profile)
```
FAX_BACKEND=phaxio
PHAXIO_API_KEY=... 
PHAXIO_API_SECRET=...
PUBLIC_API_URL=https://yourdomain.example
PHAXIO_CALLBACK_URL=https://yourdomain.example/phaxio-callback
PHAXIO_VERIFY_SIGNATURE=true
API_KEY=generate_a_strong_key
ENFORCE_PUBLIC_HTTPS=true
AUDIT_LOG_ENABLED=true
PDF_TOKEN_TTL_MINUTES=60
```
- Sinch (direct upload)
```
FAX_BACKEND=sinch
SINCH_PROJECT_ID=...
SINCH_API_KEY=...
SINCH_API_SECRET=...
# Optional regional override
# SINCH_BASE_URL=https://us.fax.api.sinch.com/v3
API_KEY=generate_a_strong_key
```
- SIP/Asterisk (self‑hosted)
```
FAX_BACKEND=sip
ASTERISK_AMI_HOST=asterisk
ASTERISK_AMI_PORT=5038
ASTERISK_AMI_USERNAME=api
ASTERISK_AMI_PASSWORD=change_me
FAX_LOCAL_STATION_ID=+15551234567
FAX_HEADER=Faxbot
API_KEY=generate_a_strong_key
```

Warnings and prompts
- Missing HTTPS on `PUBLIC_API_URL` with cloud backends → show warning, suggest tunnel/domain
- Empty `API_KEY` in production → prompt to enable auth
- Ghostscript not found for SIP/Asterisk → warn that conversion/pages may be stubbed

Learn more
- Phaxio: [Backend setup](/Faxbot/backends/phaxio-setup.html)
- Sinch: [Backend setup](/Faxbot/backends/sinch-setup.html)
- SIP/Asterisk: [Backend setup](/Faxbot/backends/sip-setup.html)
- Security: [Authentication](/Faxbot/security/authentication/), [HIPAA](/Faxbot/security/hipaa-requirements.html), [OAuth/OIDC](/Faxbot/security/oauth-setup.html)
