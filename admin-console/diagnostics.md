---
layout: default
title: Diagnostics
parent: Admin Console
nav_order: 6
permalink: /admin-console/diagnostics/
---

# Diagnostics

Run environment checks and get targeted guidance.

Checks
- API health and version
- Backend configuration
  - Phaxio: API keys present, callback URL reachability hints, signature verification state
  - Sinch: project ID and credentials present; base URL sanity
  - SIP/Asterisk: AMI connectivity and authentication, Ghostscript availability for PDF→TIFF
- Public URL
  - `PUBLIC_API_URL` presence and HTTPS enforcement if enabled
- Storage
  - Local path writable (dev)
  - Optional S3 access checks (when diagnostics enabled)
- Security posture
  - API key required, HTTPS enforced, audit logging enabled, file size limit

Actions
- “Restart API” (if allowed) to reinitialize backends after settings changes
- Copy suggested `.env` snippets for fixes

If something fails
- Follow the actionable link beside the check (e.g., Backends, Security)
- See [Troubleshooting](/Faxbot/troubleshooting/)

Related docs
- Backends: [Phaxio](/Faxbot/backends/phaxio-setup.html), [Sinch](/Faxbot/backends/sinch-setup.html), [SIP/Asterisk](/Faxbot/backends/sip-setup.html)
- Security: [Authentication](/Faxbot/security/authentication/), [HIPAA](/Faxbot/security/hipaa-requirements.html), [OAuth/OIDC](/Faxbot/security/oauth-setup.html)
- Deployment: [Guide](/Faxbot/deployment/)
- Third‑Party: [/third-party/](/Faxbot/third-party/)
