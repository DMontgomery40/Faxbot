## Faxbot Local‑Only Admin Console — Production‑Ready Plan (v1)

### Purpose
- Deliver a pragmatic, local‑only admin console that rides on the existing API.
- v1 goal: help operators do the basics without hand‑editing `.env`: keys, readiness/diagnostics, simple send, inbound view (when enabled).
- Keep it local‑only by default, safe to ship, and easy for a junior dev to implement incrementally.

### Non‑Goals (v1)
- Public/multi‑tenant UI; patient comms; analytics/CDNs.
- Editing secrets at rest (v1 reads/masks, validates, and prints copy‑paste snippets for `.env`).
- Complex webhook subscription management (defer to v2; backend not implemented yet).
- A second server/port strictly for admin (v1 uses same FastAPI process; remote access is blocked by middleware).

---

## Architecture Overview (grounded in current code)

### Runtime Topology
- Serve the UI from the existing FastAPI app under `GET /admin/ui/*` as static files.
- Reuse real endpoints that already exist: `/admin/api-keys`, `/admin/config`, `/health/ready`, `/fax`, `/fax/{id}`, and inbound endpoints.
- Local‑only in v1: a middleware 403‑blocks remote clients for any `/admin/*` path unless `ALLOW_REMOTE_ADMIN=true` AND the client IP is allow‑listed.
- No extra ports, no UDS in v1 (avoid conflicts with 8080, 3001/3002, 5038, 5060, etc.).

### Security Model (practical v1)
- Middleware blocks non‑loopback access to `/admin/*` unless `ALLOW_REMOTE_ADMIN=true` (and optionally an allow‑list).
- Auth uses existing API keys: UI asks for a token with `keys:manage` (or the bootstrap `API_KEY`) once, exchanges it for a short‑lived local session cookie via `POST /admin/session/login` (new); the cookie is HttpOnly and SameSite=Lax.
- Strict CSP and standard headers on `/admin/ui/*`.
- Secrets never re‑render; rotations display once only (matches current API key behavior).
- No service workers; PDFs/downloads use `no-store` headers.

### Settings Strategy (v1)
- Read effective settings via `/admin/config` (exists) and UI helpers.
- Do not write secrets in v1. The UI provides validated copy‑paste `.env` lines to apply manually (safer and fast to ship).
- New: `POST /admin/settings/validate` to test provider/AMI creds without saving; returns a structured report.
- v2: consider a `settings` table for non‑secret toggles; encrypted secret storage can follow with audit.

### Events & Real‑Time Updates
- The UI uses polling by default: dashboard polls `/health/ready`; job rows poll `GET /fax/{id}`.
- Advanced users can run MCP transports (HTTP/SSE/WebSocket) for assistant integrations; UI integrates these indirectly via the API.

---

## Backend API Surface (Admin Namespace)
Everything here either exists or is tightly scoped to implement.

### Authentication
- New: `POST /admin/session/login` `{ apiKey: string }` → verifies a DB key with `keys:manage` or the bootstrap `API_KEY`; sets an HttpOnly cookie; returns `{ ok: true }`.
- New: `POST /admin/session/logout` → clears the cookie; returns `{ ok: true }`.
- Middleware: block `/admin/*` for non‑loopback clients unless `ALLOW_REMOTE_ADMIN=true`.

### Settings
- `GET /admin/settings` → returns current effective settings with sensitive values masked.
- `PUT /admin/settings` (partial) → updates DB‑backed settings. Body uses a typed schema; secrets are provided in full; omitting a secret leaves it unchanged.
- `POST /admin/settings/reload` → triggers `reload_settings()` and applies changes in‑process; returns what was hot‑reloaded vs requires restart.
- `POST /admin/settings/validate` → runs connectivity checks based on provided (but not yet saved) values; e.g., Phaxio auth ping, Sinch upload test (dry), AMI login test.

### Plugins (Preview)
- `GET /plugins` → lists installed provider plugins and current enabled state (read‑only).
- `GET /plugins/{id}/config` → returns effective config for a provider (sanitized).
- `PUT /plugins/{id}/config` → persists selection/settings to the server config file.
- Notes:
  - The UI shows a Plugins tab only when the server has the plugins feature enabled.
  - Persisted changes do not immediately alter the running backend; operators apply changes during a planned restart.

Settings Validate Input (subset):
- `fax_backend: "phaxio"|"sinch"|"sip"|"test"`
- Phaxio: `phaxio_api_key`, `phaxio_api_secret`, `phaxio_status_callback_url`, `phaxio_verify_signature`
- Sinch: `sinch_project_id`, `sinch_api_key`, `sinch_api_secret`, optional `SINCH_BASE_URL`
- SIP/Asterisk: `ami_host`, `ami_port`, `ami_username`, `ami_password`, `fax_station_id`
- Security: `enforce_public_https`, `audit_log_enabled`, `audit_log_format`, `audit_log_file`, `audit_log_syslog`, `audit_log_syslog_address`
- Files: `max_file_size_mb`, `fax_data_dir`, `pdf_token_ttl_minutes`, `artifact_ttl_days`, `cleanup_interval_minutes`
- Webhooks: `webhooks_enabled`, `webhook_timeout_ms`, `webhook_max_retries`, `webhook_retry_schedule`, `webhook_enforce_https`, `webhook_default_payload`

### API Keys (existing)
- Use the existing endpoints: `POST /admin/api-keys`, `GET /admin/api-keys`, `DELETE /admin/api-keys/{keyId}`, `POST /admin/api-keys/{keyId}/rotate`.
- UI implements list/create/rotate/revoke. Tokens are shown once and never persisted by the UI.

### Webhook Subscriptions — defer to v2
- v1 shows a “coming soon” card; no server work required yet.

Delivery Contract:
- POST JSON with headers:
  - `Content-Type: application/json`
  - `X-Faxbot-Event-Id`
  - `X-Faxbot-Event-Type`
  - `X-Faxbot-Timestamp`
  - `X-Faxbot-Signature: t=<ts>, v1=<hex_hmac_sha256(timestamp + '.' + raw_body)>`
- Timeout 5s; 2xx required. Retries with exponential backoff; idempotent events.

Event Envelope:
```json
{
  "id": "evt_01H...",
  "type": "fax.job.updated",
  "created_at": "2025-09-10T12:34:56Z",
  "version": "1",
  "data": { /* minimal PHI */ }
}
```

Initial Events:
- Outbound: `fax.job.queued`, `fax.job.in_progress`, `fax.job.succeeded`, `fax.job.failed`
- Inbound (when enabled): `fax.inbound.received`, `fax.inbound.pdf_ready`, `fax.inbound.deleted`

Minimal Data Fields:
- `job_id`, `status`, `backend`, `to_masked`, `pages?`, `provider_sid?`, `pdf_url?` (short‑TTL), `error_code?`, `error_message?`

### Diagnostics
- `GET /health/ready` (exists) for summary.
- New: `POST /admin/diagnostics/run` → synchronous bounded checks (provider ping, AMI login, Ghostscript presence, `fax_data_dir` write/read). Returns JSON report; no SSE in v1.

### Jobs & Inbound Listing (Admin‑only)
- `GET /admin/fax-jobs?status=&backend=&q=&limit=&cursor=` → paginated list for UI (admin view)
- `GET /admin/fax-jobs/{id}` → details
- `GET /admin/inbound?limit=&cursor=` (when inbound enabled)

### Admin SSE Events (Local‑only)
- `GET /admin/events` (SSE): emits webhook‑identical envelopes plus admin diagnostics:
  - `admin.health.updated`, `admin.webhook.delivery`, `fax.job.*`, `fax.inbound.*`
- `Authorization: X-API-Key` (if set) required; only served on loopback.

---

## Frontend (UI) — Implementation Plan (jr‑dev friendly)

### Technology & Packaging
- SPA served as static files by FastAPI (`/admin/ui`). Preact/React OK. No external CDNs.
- ES2019+, tree‑shake, code‑split (Wizard, Keys, Jobs). No service workers.

### Global UI Principles
- **Local‑only mode indicator**: persistent badge showing binding (e.g., `127.0.0.1` / socket), backend, and security posture.
- **Secrets hygiene**: input fields for secrets masked; copy‑once behavior on creation/rotate; re‑entry required for changes.
- **Responsive first**: CSS Grid/Flex with fine‑grained breakpoints for phone/tablet/desktop.
- **Keyboard accessible**: all actions tabbable; visible focus; ARIA roles.
- **No data retention in browser**: disable form auto‑completion for secrets; no local storage of PHI.

### Navigation & Layout
- Desktop: left sidebar nav; main content area; right side panel for contextual help/validation.
- Mobile: bottom tab bar (Dashboard, Send, Jobs, Inbound, Settings). Slide‑over panels for details.
- Global toasts minimally; use inline validation near fields; error summaries at top of forms.

### Pages & Flows (v1)

1) Dashboard
- Cards: Overall Health (from `/health/ready`), Active Backend (from `/admin/config`), Recent Jobs (poll last N ids the UI knows about), Storage writeability check result.
- Actions: Open Wizard, Send Fax.

2) Setup Wizard (4 steps)
- Choose Backend → Validate Credentials → Security Quick Check → Review.
- Output: copy‑ready `.env` lines and status results; no persistence in v1.
- Step 1: Choose Backend — Phaxio / Sinch / SIP / Test. Show clear pros/cons; HIPAA notes.
- Step 2: Credentials — fields per backend with masked inputs; inline tests:
  - Phaxio: key/secret validation; callback URL preview and copy.
  - Sinch: project/key/secret; region base URL; upload test (dry run).
  - SIP: AMI host/port/user/password; station ID; test AMI login.
- Step 3: Security — set `API_KEY`, `ENFORCE_PUBLIC_HTTPS`, HMAC verification flags; audit logging options; retention and token TTL.
- Step 4: Webhooks — create subscription(s); reveal `secret_once`; send test; show delivery results.
- Step 5: Inbound (optional) — enable; set storage backend; retention; display provider callback URLs to configure.
- Step 6: Review & Apply — show diff; save to DB; call `/admin/settings/reload`; show post‑apply health.

3) Send Fax
- File picker (PDF/TXT). Client‑side size/type checks. Submit to `POST /fax` with the admin session cookie (server proxies through with the admin key) or with a scoped send key pasted once.
- Poll `GET /fax/{id}` for status.

4) Outbound Jobs
- Table/list with filters: status, backend, date range, search by job id/last 4 of number.
- Row details: status timeline, pages (if known), error (redacted), provider SID (optional).
- Actions: copy job id, copy tokenized PDF link (if enabled), re‑send (creates new job), delete artifacts (respect retention policy).

5) Inbound Inbox (when enabled)
- List inbound with masked fields; view/download PDF using the existing token or API key path.
- No editing of routing/rules in v1.

6) API Keys
- List keys with name, scopes, created_at, last_used_at (if tracked).
- Create flow: name + scopes; show token once with copy; warn about secure storage.
- Rotate/Delete with confirmation; audit logged.

7) Webhooks — v2 placeholder
- Show an informational card; link to docs; no actions.

8) Settings
- Organized tabs: Backend, Security, Files/Retention, Logging, Advanced.
- All secret fields masked with explicit “Update secret” flow.
- Validate button runs `/admin/settings/validate`; results shown inline.
- Save triggers PUT and `reload`, with applied vs requires‑restart list.

9) Diagnostics
- Run full suite: provider ping, AMI connect, Ghostscript check, storage write/read, clock drift.
- Stream results via SSE with pass/fail icons and remediation tips.

10) About / Version
- Show Faxbot version, build info, licenses; copy diagnostics.

### Components & Interactions
- SecretInput: masked by default; “show” toggle only for current session; copy button (where applicable) never re-renders stored secrets.
- PhoneInput: formats, validates, and normalizes to E.164 where possible.
- FileDrop: accept `.pdf`, `.txt`; surface size/type errors early.
- DataTable: virtualized lists for performance; row actions accessible.
- ConfirmationModal: destructive actions require confirm with job id or keyword.

### Responsive Layout Spec
- Breakpoints: 0–479 (phone), 480–767 (large phone), 768–1023 (tablet), 1024–1439 (small desktop), 1440+ (desktop).
- Grid usage:
  - Dashboard: responsive grid; cards auto‑flow 1–3 per row depending on width.
  - Tables: collapse to stacked cards on phone; show critical fields first.
- Spacing scale: 4px units; tap targets ≥44px.
- Dark theme palette (WCAG AA+):
  - Background #0B0F14; Surface #121821; Text #E6EAF0 (primary), #A8B0BB (secondary); Accent #3BA0FF; Danger #FF5C5C; Success #2EBE7E; Warning #FFB020.

---

## Validation & Health Checks (Backend Logic)

### Phaxio
- Validate credentials by calling `GET /v2/account/status` with Basic auth.
- Verify callback URL reachability (optional HEAD) and display the full `phaxio_status_callback_url` with `?job_id={job_id}` guidance.

### Sinch Fax v3
- Validate project/key/secret by attempting a small, local PDF upload to `/files` (dry or tiny sample) or creating a test fax in sandbox if supported.
- Region base URL check; fallback between default bases.

### SIP/Asterisk
- AMI connect/login; check `faxout` context presence if introspection is available; warn if `ami_password` is default.
- Ghostscript check (`gs` presence) for PDF→TIFF conversion.

### Storage & Files
- Write/read/delete test in `fax_data_dir` with current perms; warn if not writable.

### Time & TLS
- Optionally check NTP sync; warn on >2m skew (affects signatures, tokens).
- If `enforce_public_https` is true and backend is Phaxio, warn on `PUBLIC_API_URL` http scheme.

---

## Event Model — v2 (SSE), not required for v1

Headers (webhooks only): as defined above. SSE uses `event:` and `data:` lines with the envelope.

Outbound Lifecycle Examples:
```json
{ "id": "evt_01H...", "type": "fax.job.queued", "version": "1", "created_at": "2025-09-10T12:00:00Z", "data": { "job_id": "abc123", "backend": "phaxio", "to_masked": "*******4567" } }
```
```json
{ "id": "evt_01H...", "type": "fax.job.succeeded", "version": "1", "created_at": "2025-09-10T12:01:23Z", "data": { "job_id": "abc123", "pages": 2, "backend": "phaxio" } }
```

Inbound Example (when enabled):
```json
{ "id": "evt_01H...", "type": "fax.inbound.received", "version": "1", "created_at": "2025-09-10T12:05:00Z", "data": { "inbound_id": "in_789", "from_masked": "*******9876", "to": "+15551234567", "pages": 3 } }
```

Admin Delivery Result Example:
```json
{ "id": "evt_01H...", "type": "admin.webhook.delivery", "version": "1", "created_at": "2025-09-10T12:00:10Z", "data": { "subscription_id": "sub_123", "event_id": "evt_01H...", "status": 200, "latency_ms": 142 } }
```

---

## Implementation Guidance (server)

### Minimal server additions (v1)
- Static UI: mount under `/admin/ui` using `StaticFiles`.
- Middleware: loopback‑only enforcement for `/admin/*` unless allow‑listed.
- New endpoints:
  - `POST /admin/session/login` `{ apiKey }` → set HttpOnly cookie if key has `keys:manage` (or matches bootstrap env `API_KEY`).
  - `POST /admin/session/logout` → clear cookie.
  - `POST /admin/settings/validate` → run provider/AMI pings with supplied values; do NOT persist.
  - `POST /admin/diagnostics/run` → run bounded checks; return JSON report.
- No DB settings table in v1; no webhooks queue in v1; no SSE in v1.

### UI Build & UX
- TypeScript, Vite build; strict ESLint; unit tests for critical helpers (phone formatting, size checks, masking).
- CSS: custom properties for dark theme; container queries where available; Grid/Flex layouts with graceful degradation.
- Accessibility: labels, aria‑describedby for help text, focus management in dialogs.

### Mobile & Tablet
- Bottom nav with 5 tabs; swipe‑back support; big tap targets; on‑screen keyboard types for phone inputs.
- Drag‑drop replaced with prominent upload button; show file name/size/type.

### Testing Strategy (acceptance for v1)
- Unit tests: validators, API clients, login flow, diagnostics report shapes.
- E2E (local Playwright): wizard validate (per backend), key create/rotate/revoke, send fax (FAX_DISABLED=true), inbound list/download (when enabled).
- Security checks: verify CSP headers; loopback middleware (remote request gets 403).

### Observability (Local)
- Minimal delivery logs with metadata only; toggle debug mode in dev.
- Health banner if admin API detects insecure config (e.g., admin served on 0.0.0.0).

---

## Risk Register & Mitigations (functional)
- Accidental exposure: 403 remote access to `/admin/*` unless `ALLOW_REMOTE_ADMIN=true`; show a big “local‑only” banner.
- Secret leakage: mask outputs; never persist secrets client‑side; rotations display tokens once; prefer session cookie over putting the API key into fetch headers.
- Memory/FD leaks: no SSE in v1; polling only; job polling capped and canceled on navigation.
- Port conflicts: reuse 8080; no new ports in v1.

---

## Rollout Plan (Phased)
1) Minimal server endpoints + UI shell (Keys, Send, Wizard validate, Diagnostics run); loopback middleware; session login.
2) Inbound list/view when enabled; basic settings reload; limited non‑secret toggles.
3) SSE for live updates; webhook UI after backend queue exists.
4) Settings DB for non‑secrets; optional encryption for secrets; LAN allowlist/pin.

This plan is grounded in the current codebase, avoids imaginary endpoints, and is sequenced so a junior implementer can deliver v1 safely and incrementally.
