---
layout: default
title: Diagnostics Matrix
parent: Admin Console
nav_order: 5
permalink: /admin-console/diagnostics-matrix/
---

# Diagnostics Matrix

Each check maps to a fix, relevant environment variables, and the UI path to change it.

Backend: Phaxio
- Missing API key/secret
  - Fix: set `PHAXIO_API_KEY`, `PHAXIO_API_SECRET`
  - UI: Settings → Backend → Phaxio
- Callback unreachable or unset
  - Fix: set `PHAXIO_CALLBACK_URL` (or `PHAXIO_STATUS_CALLBACK_URL`) to `<PUBLIC_API_URL>/phaxio-callback`
  - UI: Settings → Backend → Phaxio; also verify `PUBLIC_API_URL` in Settings → Security
- Signature verification disabled
  - Fix: enable `PHAXIO_VERIFY_SIGNATURE=true`
  - UI: Settings → Backend → Phaxio (Verify signatures)

Backend: Sinch
- Missing project/credentials
  - Fix: set `SINCH_PROJECT_ID`, `SINCH_API_KEY`, `SINCH_API_SECRET`
  - UI: Settings → Backend → Sinch
- Wrong region/base URL
  - Fix: set `SINCH_BASE_URL` to the correct regional endpoint
  - UI: Settings → Backend → Sinch

Backend: SIP/Asterisk
- AMI unreachable or auth failed
  - Fix: verify `ASTERISK_AMI_HOST`, `ASTERISK_AMI_PORT`, `ASTERISK_AMI_USERNAME`, `ASTERISK_AMI_PASSWORD`
  - UI: Settings → Backend → SIP/Asterisk
- TIFF conversion unavailable
  - Fix: install Ghostscript (`gs`) on API host
  - UI: N/A — server dependency; rerun Diagnostics after install

Public URL / HTTPS
- Missing or HTTP in production
  - Fix: set `PUBLIC_API_URL` to your HTTPS domain; enable `ENFORCE_PUBLIC_HTTPS=true`
  - UI: Settings → Security

Security posture
- API key not required
  - Fix: set `API_KEY` to a strong value; restart if necessary
  - UI: Settings → Security
- Audit logging off (HIPAA profile)
  - Fix: `AUDIT_LOG_ENABLED=true`; optionally configure file/syslog
  - UI: Settings → Security → Audit

Storage (Inbound)
- Local path unwritable
  - Fix: check `FAX_DATA_DIR` permissions or switch to S3
  - UI: Settings → Storage
- S3 not accessible
  - Fix: verify bucket/region/prefix/endpoint; use role/env credentials; (optional) enable diagnostics for HeadBucket
  - UI: Settings → Storage

File limits
- Uploads rejected as too large
  - Fix: raise `MAX_FILE_SIZE_MB` and communicate the limit in the UI
  - UI: Settings → Security → File limits

Actions
- After each change, click “Apply & Reload” and rerun Diagnostics
- Use “Restart API” when prompted (if enabled) for backend client reinitialization

