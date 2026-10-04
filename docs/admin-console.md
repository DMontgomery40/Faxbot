
# Admin Console

<div class="grid cards" markdown>

- :material-wrench: **Setup Wizard**  
  Select backend, paste creds, apply.  
  [Open](admin-console/setup-wizard.md)

- :material-cog: **Settings**  
  Backend/security/storage controls with helper text.  
  [Open](admin-console/settings.md)

- :material-stethoscope: **Diagnostics**  
  Health checks and actionable fixes.  
  [Open](admin-console/diagnostics.md)

- :material-key-variant: **API Keys**  
  Mint, rotate, revoke; scopes and rate limits.  
  [Open](admin-console/api-keys.md)

- :material-puzzle-outline: **Plugin Registry**\
  Discover providers and configure installed plugins.\
  [Open](plugins/registry.md)

</div>

The Admin Console lets you manage keys, jobs, inbound inbox, diagnostics, and settings without editing `.env` by hand.

- Local‑only by default; access is restricted to loopback in current builds
- Works with any backend (Phaxio, Sinch, SIP/Asterisk, SignalWire)
- Provides copy‑ready configuration after validation

## Usage

- Access at `http://localhost:8080/admin/ui/` when the API is running
- If the UI is unavailable, set the deployment gate `ENABLE_LOCAL_ADMIN=true` and install the built UI at `/app/admin_ui/dist` in the container or `api/admin_ui/dist` locally, then restart the serving API. These mount/gate inputs remain deployment environment settings; canonical runtime edits use [Settings](admin-console/settings.md).
- Explore tabs for Dashboard, Send, Jobs, Inbound, Keys, Settings, Diagnostics

### Plugins (preview)
- Enable v3 plugins through canonical Settings on an existing installation and inspect the apply state. The tab appears when the active feature is enabled.
- Plugins edits desired provider settings and direction selection with a loaded revision guard. Its result identifies active or pending changes; it does not write a separate authoritative runtime JSON file.

## Demo (Simulated)

- Hosted demo with simulated data: https://faxbot.net/admin-demo/
- No external calls; intended for showcasing the workflow

## Desired settings, activation and recovery

Setup Wizard and Settings load canonical desired values and the revision they edit. Apply saves only changed fields with that revision; conflicts retain the draft for explicit reload/review. The response distinguishes active values from desired changes pending restart. For pending changes, stop every API worker and restart the installation, then verify the active/desired identity. A readonly reload does not activate pending configuration, and restarting one process while other workers remain running is insufficient.

A redacted export is a desired template, not persistence or a complete backup. Settings can write a private recovery environment file; it does not promote pending settings. Preserve the database, installation encryption key and document artifacts for recovery. Environment and legacy JSON inputs bootstrap an installation without canonical state; subsequent `.env` edits do not override its saved revision. See [Settings](admin-console/settings.md) and [Setup](admin-console/setup-wizard.md).

The **Restart now** button in the Settings restart message and the Diagnostics **Restart API** action stop the API process when restarts are allowed. With Docker Compose the `api` service starts again by itself (`restart: unless-stopped`). Where several API processes run, arrange an installation-wide stop and start through the process manager instead.

## Storage (S3)

- In Settings, select S3 for artifact storage and edit its values (`S3_BUCKET`, `S3_REGION`, optional `S3_PREFIX`, `S3_ENDPOINT_URL`, `S3_KMS_KEY_ID`).
- IAM credentials must come from the runtime (environment or role). The Admin Console does not store or display secrets.
- Validate S3:
  - Enable `ENABLE_S3_DIAGNOSTICS=true` on the API to allow Diagnostics to `HeadBucket` and surface `checks.storage.accessible`.
  - Otherwise, Diagnostics will show only presence checks.
  - Best practice: apply settings, then run Diagnostics to verify access, and perform an end‑to‑end inbound test.

## Dashboard & Diagnostics

- Dashboard describes the active outbound configuration and durable delivery counts. Desired pending settings can differ from the running provider.
- Diagnostics runs a comprehensive check (backend credentials/config, storage, inbound flags, security posture) and shows recommendations.

### Under the Hood
- The console reads settings from `GET /admin/settings` and health from `GET /admin/health-status`
- Apply calls `PUT /admin/settings` with changed fields and the loaded desired revision; `_meta` reports active/pending state
- `POST /admin/settings/reload` only reads durable state
- `POST /admin/settings/persist` writes a recovery file; it does not become the authoritative settings store
- Jobs table uses admin‑scoped endpoints (`/admin/fax-jobs*`) with masked phone numbers
- Jobs lists each fax by its number, state and provider name (for example **SIP trunk (Asterisk)**); the job ID appears only in Job Details, where it can be copied. After a send, the confirmation names the number ("Fax queued for +12015550123.") and **Follow it in Jobs** opens that fax's details.

## Inbound Controls (v2)

- The Inbox shows each received fax with its email delivery status and a **Retry delivery** action; email delivery is set up in Settings. See [Intake](operations/intake.md).
- Toggle inbound receiving on/off and configure retention/token TTL in Settings.
- The Inbox shows when each fax arrived (the provider's time when known) and Unknown for a number nobody reported. With your own SIP trunk receiving, it shows one line, "Receiving over your SIP trunk: ready." or why a received fax could not be handed to Faxbot, with **Open trunk settings**; there is no URL or dialplan to configure. Phaxio and Sinch show the callback URL to enter in their consoles.
- Backend-specific auth:
  - SIP/Asterisk: Faxbot creates the secret Asterisk sends with each received fax; a value set in Settings or as `ASTERISK_INBOUND_SECRET` in `.env` is used instead.
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
  - Pending MCP/OAuth changes require every API worker to stop and the installation restart; verify active/desired identity afterward.
