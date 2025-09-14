---
layout: default
title: Settings
parent: Admin Console
nav_order: 2
permalink: /admin-console/settings/
---

# Settings

Adjust backend, security, MCP, and storage without manual edits.

Quick start tips
- New to Faxbot? Use the [Setup Wizard](/Faxbot/admin-console/setup-wizard/) first — it walks you through the minimum you need.
- No domain? Pick “Sinch (Direct Upload)” in the wizard or here under Backend → Sinch; you can send without a public URL.
- Phaxio sign‑ups redirect to Sinch (expected). Use: https://dashboard.sinch.com/signup

Backend
- Select active backend: sinch | documo | phaxio | sip
- Phaxio
  - `PHAXIO_API_KEY`, `PHAXIO_API_SECRET` (secret inputs)
  - `PHAXIO_CALLBACK_URL` or `PHAXIO_STATUS_CALLBACK_URL`
  - Toggle “Verify signatures” → `PHAXIO_VERIFY_SIGNATURE=true`
- Sinch
  - `SINCH_PROJECT_ID`, `SINCH_API_KEY`, `SINCH_API_SECRET`
  - Optional `SINCH_BASE_URL`
- Documo (mFax)
  - `DOCUMO_API_KEY`
  - Optional `DOCUMO_SANDBOX=true` and `DOCUMO_BASE_URL`
- SIP/Asterisk
  - `ASTERISK_AMI_HOST`, `ASTERISK_AMI_PORT`, `ASTERISK_AMI_USERNAME`, `ASTERISK_AMI_PASSWORD`
  - Presentation: `FAX_LOCAL_STATION_ID`, `FAX_HEADER`

Security
- Require API key: sets `API_KEY` and enables auth on REST endpoints
- Enforce HTTPS for cloud fetches: `ENFORCE_PUBLIC_HTTPS`
- Audit events: `AUDIT_LOG_ENABLED`, format/file/syslog options
- File limits: `MAX_FILE_SIZE_MB` (default 10 MB)

MCP (Tools)
- Enable SSE (Python/Node) and configure OAuth2/JWT for HIPAA use
- Copy‑ready configs for popular assistants

Storage (Inbound)
- Local (dev only) or S3/S3‑compatible with KMS support
- Fields: bucket, region, prefix, endpoint URL, KMS Key ID (secrets via environment/role)
 - See also: AWS S3 SSE‑KMS (https://docs.aws.amazon.com/AmazonS3/latest/userguide/UsingKMSEncryption.html)

Persistence
- Enable persisted `.env` on startup (local only) to reload saved settings
- Export `.env` for review in change control systems

Learn more
- Backends: [Phaxio](/Faxbot/backends/phaxio-setup.html), [Sinch](/Faxbot/backends/sinch-setup.html), [SIP/Asterisk](/Faxbot/backends/sip-setup.html)
- Security: [Authentication](/Faxbot/security/authentication/), [HIPAA](/Faxbot/security/hipaa-requirements.html), [OAuth/OIDC](/Faxbot/security/oauth-setup.html)
- Third‑Party: [/third-party/](/Faxbot/third-party/)
