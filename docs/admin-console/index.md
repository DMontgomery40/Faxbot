---
layout: default
title: Admin Console
nav_order: 4
permalink: /admin-console/
---

# Admin Console

> The local Admin Console helps you manage keys, jobs, inbound inbox, diagnostics, and settings without editing `.env` files by hand.

- Local‑only by default; protect with API keys and loopback restrictions
- Works alongside any backend (Phaxio, Sinch, SIP/Asterisk)
- Provides copy‑ready configuration lines after validation

## Usage

- Access at `http://localhost:8080/admin/ui/` when the API is running
- Use an admin‑scoped API key to log in (or bootstrap key in development)
- Explore tabs for Dashboard, Send, Jobs, Inbound, Keys, Settings, Diagnostics

## Demo (Simulated)

- Hosted demo with simulated data: https://faxbot.net/admin-demo/
- No external calls; intended for showcasing the workflow

## Live Apply & Export

- Setup Wizard: choose a backend (Phaxio/Sinch/SIP), enter credentials, pick security defaults (require API key, enforce HTTPS, audit logging), then click “Apply & Reload”.
  - Changes apply in‑process immediately.
  - Click “Generate .env” to export a snippet for persistence across restarts.
- Settings: quick edits for backend/security and selected provider fields.
  - Click “Apply & Reload” to take effect immediately.
  - Backend/storage changes may require a restart to initialize provider clients (e.g., Asterisk AMI) or swap storage drivers safely.

## Persisted Settings (v2)

- Enable loading of a server-side .env on startup: toggle “Load persisted .env at startup” in Settings (sets ENABLE_PERSISTED_SETTINGS=true).
- Save the current configuration to a persisted file by clicking “Save .env to server”.
  - Default path: /faxdata/faxbot.env (lives on the `faxdata` volume).
  - You can still export and download the .env if you prefer manual review.
- Notes
  - When enabled, the API loads values from the persisted file before constructing settings, overriding process environment.
  - Keep this feature local-only and behind the Admin Console gate.

## Restart (Optional)

- If `ADMIN_ALLOW_RESTART=true`, the Diagnostics page shows a “Restart API” button.
  - This triggers a controlled process exit so your container manager (e.g., Docker) restarts the API.
  - If the flag is not set, the button returns “Restart not allowed”.

## Storage (S3)

- To use S3 for inbound artifacts, set `STORAGE_BACKEND=s3` and S3 values (`S3_BUCKET`, `S3_REGION`, optional `S3_PREFIX`, `S3_ENDPOINT_URL`, `S3_KMS_KEY_ID`).
- IAM credentials must come from the runtime (environment or role). The Admin Console does not store or display secrets.
- Validate S3:
  - Enable `ENABLE_S3_DIAGNOSTICS=true` on the API to allow Diagnostics to `HeadBucket` and surface `checks.storage.accessible`.
  - Otherwise, Diagnostics will show only presence checks.
  - Best practice: apply settings, then run Diagnostics to verify access, and perform an end‑to‑end inbound test.

## Dashboard & Diagnostics

- Dashboard shows the live backend (phaxio/sinch/sip) and simple queue stats. After applying settings, it reflects the new backend.
- Diagnostics runs a comprehensive check (backend credentials/config, storage, inbound flags, security posture) and shows recommendations.

## Inbound Controls (v2)

- Toggle inbound receiving on/off and configure retention/token TTL in Settings.
- Backend-specific auth:
  - SIP/Asterisk: set `ASTERISK_INBOUND_SECRET` for the private `/_internal/asterisk/inbound` route.
  - Phaxio: enable HMAC verification for inbound webhooks.
  - Sinch: configure Basic auth and/or HMAC verification for inbound callbacks.

## Outbound PDFs (v2)

- From Jobs, open a job to view details and download the outbound PDF (admin-only). The API generates a PDF per job before dispatching to the selected backend.
## MCP (v2)

- Embedded Python MCP SSE server is available under `/mcp/sse`.
- In the UI (MCP tab), enable SSE and optionally require OAuth/JWT.
- Health check: “SSE Healthy” chip reflects `/mcp/sse/health` status.
- “Claude Desktop Config” block provides a copy‑ready config snippet.
- Notes
  - For HIPAA, enable OAuth/JWT and configure issuer/audience/JWKS.
  - When disabled, SSE runs without auth for local development only.
  - Changing MCP flags requires an API restart (use Restart in UI).
