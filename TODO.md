# Missing Features & Mandates — Running List

Updated: 2025-09-17

Purpose: While auditing AGENTS.md against the codebase and syncing the Admin Console and vendor demo, track missing features, drifts, and UX mandates here. Always add new items at the top. Close items with a short note and file/commit reference.

- Admin Console nav parity and cleanup
  - Remove Plugin Builder from UI navigation entirely; keep manifest runtime only. Verify no routes point to `api/admin_ui/src/components/PluginBuilder.tsx`.
  - Group navigation as: Dashboard; Settings → Setup, Settings, Keys, MCP; Tools → Terminal, Diagnostics, Logs, Plugins (feature‑gated), Scripts & Tests.
  - Ensure mobile responsiveness and legibility across common breakpoints for all Tools sub‑tabs (Terminal, Diagnostics, Logs, Plugins, Scripts & Tests).

- Scripts & Tests (backend‑aware)
  - Add Sinch helper card(s) (credentials check, base URL hint) and SignalWire + FreeSWITCH preview helpers alongside existing Phaxio/SIP. File: `api/admin_ui/src/components/ScriptsTests.tsx`.
  - Enforce backend isolation: only show cards and guidance for the active backend; hide inbound helpers unless `INBOUND_ENABLED=true`.
  - Prevent concurrent runs across cards (global lock) and reflect per‑card disable state clearly.
  - Replace any placeholder/cute labels; no “GUI‑first” banners; keep copy plain with tooltip + “Learn more”.
  - Add backend‑aware remediation links in Jobs detail for failed jobs (Phaxio/Sinch/SIP/FS specific). File: `api/admin_ui/src/components/JobsList.tsx`.

- Terminal (local‑only)
  - Confirm Tools → Terminal is present and functional; requires admin key and `ENABLE_LOCAL_ADMIN=true`.
  - Improve Safari input handling by consolidating key events (migrate any lingering onKeyPress to onKeyDown). File: `api/admin_ui/src/components/Terminal.tsx`.
  - Seed initial prompt/output (e.g., welcome + `pwd && ls`) and auto‑fit at mount. Also replicate fake FS seeding in vendor demo.
  - Verify server WS endpoint gating/requirements and helpful error when unavailable. Files: `api/app/main.py:/admin/terminal`, `api/app/terminal.py`.
  - Replace login input onKeyPress in `api/admin_ui/src/App.tsx` with onKeyDown for cross‑browser consistency.

- Diagnostics
  - Extend `/admin/diagnostics/run` with SignalWire and FreeSWITCH checks (preview) to match UI hints. File: `api/app/main.py`.
  - Ensure “Open Settings” button routes to the grouped Settings screen (fixed; re‑verify).
  - Add provider‑specific troubleshooting links; keep backend isolation.

- Plugins (v3)
  - Show Tools → Plugins only when `FEATURE_V3_PLUGINS=true`; ensure no Plugin Builder routes or mentions remain in UI.
  - Validate curated registry parity with server `/plugin-registry`; ensure demo mocks mirror current curated entries.
  - Security passes: manifests respect allowed domains, timeouts, HTTPS in HIPAA; redact secrets in UI.
  - Add redaction policy in manifest runtime to scrub sensitive fields from any UI‑surfaced debug output.

- API Admin Actions & Terminal tests
  - Add tests for `GET /admin/actions` and `POST /admin/actions/run` (gating via `ENABLE_ADMIN_EXEC`, allowlist, backend gating, timeout). New file: `api/tests/test_admin_actions.py`.
  - Add tests for `/admin/terminal` WebSocket auth paths (env key vs DB key with `keys:manage`), local‑only enforcement, requirement failure messaging. New file: `api/tests/test_terminal_ws.py`.

- Inbound receiving UX & endpoints
  - UI: Toggle + storage/KMS guidance, token TTL controls, list/detail with secure download; provider callback guidance (`/phaxio-inbound`, `/sinch-inbound`, SIP internal). File: `api/admin_ui/src/components/Inbound.tsx`.
  - API: Re‑verify `/_internal/asterisk/inbound`, `/phaxio-inbound`, `/sinch-inbound` logic, token TTL defaults, retention cleanup. File: `api/app/main.py`.

- Docs (Jekyll site parity)
  - Add “Scripts & Tests” page documenting all scripts under `scripts/` and `node_mcp/scripts/` with usage; keep in parity with main.
  - Add “Terminal” page (Admin Console local terminal: security, gating, usage). Link from Tools → Terminal help.
  - Remove any references to Plugin Builder UI; prefer manifest runtime notes.
  - Ensure all UI help links use `DOCS_BASE_URL` and are backend‑specific.

- Release hygiene (main branch)
  - Strip internal planning artifacts from main/origin: `AGENTS.md`, `USER_TODO.md`, `v3_plans/`, `@v3_console_plans/`, any planning notes and TODOs; keep only public docs (Jekyll site) in main. Provide a release script/Make target that assembles a clean tree.
  - Verify no internal planning docs ship in Docker images.

- Vendor admin demo (faxbot.net)
  - Ensure nav parity and Tools sub‑tabs mirror Console: includes Terminal (demo), Diagnostics, Logs, Plugins (if feature‑gated), Scripts & Tests (backend‑aware sample).
  - Seed demo Terminal with a fake filesystem and initial `ls` output. Validate Plugins demo list matches curated registry.

- Security & logging
  - Confirm no secrets or PHI are logged in UI/network; UI shows IDs/metadata only. Ensure Action outputs are truncated and redacted.
  - Confirm strict HTTPS and HMAC verification defaults for cloud backends in HIPAA mode.

- SDKs & OpenAPI
  - Verify SDKs are at version 1.1.0 and aligned with current `/openapi.json`. Ensure Admin UI types match spec; avoid drift.

- Full AGENTS.md audit
  - Produce a line‑by‑line checklist mapping each statement to concrete code/files and note deltas or TODOs. Save at `docs/AGENTS_AUDIT.md`; keep updated.

— End of current list —
