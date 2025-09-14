---
layout: default
title: Troubleshooting
nav_order: 80
permalink: /troubleshooting/
---

# Troubleshooting

Common errors
- 401 Unauthorized
  - Set `API_KEY` on the server and send `X-API-Key` on every request
- 415 Unsupported Media Type
  - Only PDF and TXT are allowed; convert images to PDF first
- 413 Payload Too Large
  - Default max is `MAX_FILE_SIZE_MB=10`; reduce file or increase limit
- 404 Job not found
  - Verify the job ID and that the API is using the same database/volume

Statuses
- Stuck in queued
  - Phaxio/Sinch: callback not reaching your API. Verify `PUBLIC_API_URL` and callback path, confirm HTTPS, check provider dashboard for webhook attempts
  - SIP/Asterisk: AMI events not received. Check AMI connectivity and that dialplan posts `UserEvent(FaxResult, ...)`
- Immediate failed
  - Phaxio/Sinch: credential issue or content fetch failure (Phaxio). Recheck keys and that `GET /fax/{id}/pdf?token=...` is reachable
  - SIP/Asterisk: missing Ghostscript or TIFF conversion; install `ghostscript`

Phaxio callback
- Ensure `PHAXIO_STATUS_CALLBACK_URL` (or `PHAXIO_CALLBACK_URL`) set to `<PUBLIC_API_URL>/phaxio-callback`
- Include `?job_id=<id>` on the callback URL so the API can correlate the job
- If `PHAXIO_VERIFY_SIGNATURE=true`, confirm the `X-Phaxio-Signature` header is present and the secret is correct

Sinch errors
- “create fax error 4xx/5xx”
  - Check `SINCH_PROJECT_ID`, key/secret, and service region/base URL
  - Make sure the project has fax enabled

SIP/Asterisk
- AMI login failed
  - Validate host/port/user/password; confirm firewall and that the AMI service is reachable only inside your network
- No pages recorded
  - Ensure dialplan emits `UserEvent(FaxResult, Pages:...)`; page count is optional and may be provider‑reported later

Diagnostics
- Use Admin Console → Diagnostics to run checks and follow provided fix links
- API: `GET /health` → `{ status: "ok" }`
- Logs show warnings for missing API key or insecure public URL when enforcement is on

More help
- [Backends](/Faxbot/backends/)
- [Deployment](/Faxbot/deployment/)
- [Security](/Faxbot/security/)
