# AGENTS.md Line‑by‑Line Audit (2025‑09‑17)

This audit maps each claim in AGENTS.md to concrete code, files, or behaviors in the repository. Status values:
- VERIFIED — implemented and matched
- PARTIAL — implemented with gaps noted
- DRIFT — statement differs from current code; delta called out
- MISSING — planned or stated but not present

References use clickable file paths with single start lines per CLI rules.

## Identity & Scope
- Name “Faxbot” and “faxbot‑mcp” only — VERIFIED
  - Evidence: api/admin_ui/src/App.tsx:321, sdks/node/README.md:22, sdks/python/README.md:22
  - No “OpenFax”/“twilio‑fax” in code. Note: api/CHANGELOG.md mentions rename only (contextual).
- Production/PHI intent — VERIFIED (controls present)
  - HTTPS enforcement, HMAC verification, auth, no secret logging in UI; see api/app/config.py:71, api/app/main.py:2321, api/admin_ui/src/components/JobsList.tsx:262

## Architecture (v3 Plugins + Backends)
- v3 plugin runtime + config store — VERIFIED
  - Runtime: api/app/plugins/http_provider.py:51, api/app/plugins/http_provider.py:218
  - Config store: api/app/plugins/config_store.py:24
  - Endpoints (feature‑gated): /plugins, /plugins/{id}/config, /plugin‑registry
    - api/app/main.py:3203, api/app/main.py:3210, api/app/main.py:3337
  - Config path env: api/app/config.py:161 (FAXBOT_CONFIG_PATH)
- Multiple backends via plugins/providers — VERIFIED
  - Phaxio: api/app/phaxio_service.py:1, cloud flow in main send path api/app/main.py:2038
  - Sinch: api/app/sinch_service.py:1, api/app/main.py:2045
  - SignalWire (preview): api/app/signalwire_service.py:1, callback api/app/main.py:3351
  - SIP/Asterisk: AMI originate api/app/ami.py:1, api/app/main.py:2108
  - FreeSWITCH (preview): originate helpers api/app/freeswitch_service.py:1, result hook api/app/main.py:3406
- AI integration (MCP Node + Python) — VERIFIED
  - Node stdio/http/sse/ws: node_mcp/src/servers/stdio.js:3, node_mcp/src/servers/http.js:15, node_mcp/src/servers/sse.js:7, node_mcp/src/servers/ws.js:7
  - Python stdio/SSE: python_mcp/stdio_server.py:1, python_mcp/server.py:1
  - Notes about legacy: no api/mcp_*.js present — VERIFIED by absence
- SDKs (Node + Python) with identical API — VERIFIED (v1.1.0)
  - Versions: sdks/node/package.json:3, sdks/python/setup.py:18
  - API surface: sdks/node/index.js:27, sdks/python/faxbot/__init__.py:30
  - OpenAPI source at /openapi.json via FastAPI: api/app/main.py:45

## Admin Console First (GUI Mandate)
- All capabilities operable via Admin Console — PARTIAL
  - Send, Jobs, Inbound, Settings, Keys, Diagnostics, Logs — present: api/admin_ui/src/App.tsx:367
  - Scripts & Tests (backend‑aware) — present but missing Sinch/SignalWire/FreeSWITCH helpers (see below)
  - Admin Actions (allowlisted checks) — present: api/app/main.py:1279, api/admin_ui/src/components/ScriptsTests.tsx:281
- Contextual help + docs links use docs base — VERIFIED
  - Admin config exposes docs base: api/app/main.py:502, api/app/main.py:517
  - UI consumes docs_base: api/admin_ui/src/App.tsx:452
- Backend isolation in UI — PARTIAL
  - Scripts & Tests and Diagnostics show backend‑specific info: api/admin_ui/src/components/ScriptsTests.tsx:269, api/admin_ui/src/components/Diagnostics.tsx:553
  - Missing helper cards for Sinch/SignalWire/FreeSWITCH (see TODO)
- Mobile‑first polish — PARTIAL
  - Responsive tabs/breakpoints: api/admin_ui/src/App.tsx:351
  - Some components still need compact copy and spacing review (UX polish task)

## v3 UI Additions
- Plugins tab (feature‑gated) — VERIFIED
  - UI gating: api/admin_ui/src/App.tsx:447
  - Server gating: api/app/main.py:3339
- Curated registry search — VERIFIED
  - Endpoint: /plugin‑registry: api/app/main.py:3337
- Contextual help per provider — PARTIAL
  - Present in PluginConfigDialog: api/admin_ui/src/components/PluginConfigDialog.tsx:56, 79, 97
- Scripts & Tests + Terminal — VERIFIED/PARTIAL
  - Terminal present: api/admin_ui/src/components/Terminal.tsx:17; WS: api/app/main.py:3442
  - Scripts & Tests present but helper coverage incomplete (no Sinch/SignalWire/FreeSWITCH cards): api/admin_ui/src/components/ScriptsTests.tsx:250

## Acceptance Criteria (per screen)
- Inline explanations and a docs link — PARTIAL
  - Many components include captions/tooltips; ensure each field follows through (spot‑check OK)
- Validation and helpful errors — PARTIAL
  - API returns structured HTTP errors; some UI flows surface messages (e.g., Diagnostics), but missing targeted remediation links in Jobs detail
- Jobs failures surface troubleshooting links — MISSING
  - JobsList modal lacks backend‑specific remediation links: api/admin_ui/src/components/JobsList.tsx:274

## Provider Backends
- Phaxio (Cloud) — VERIFIED
  - Env: AGENTS matches config keys; settings: api/app/config.py:64–76
  - Send path uses PDF URL token: api/app/main.py:2376, api/app/main.py:2260
  - HMAC callback verification: api/app/main.py:2319
- SIP/Asterisk (Self‑Hosted) — VERIFIED
  - AMI config envs: api/app/config.py:51–55
  - TIFF conversion for SIP: api/app/main.py:2064
  - Internal inbound endpoint with secret: api/app/main.py:2747
- Sinch Fax v3 — VERIFIED (send; inbound present)
  - Settings: api/app/config.py:77–83, inbound auth: api/app/config.py:129–131
  - Inbound handler: api/app/main.py:2955
- SignalWire (Cloud, preview) — VERIFIED (callback path)
  - Service: api/app/signalwire_service.py:1; callback: api/app/main.py:3351
- FreeSWITCH (Self‑Hosted, preview) — VERIFIED (result hook)
  - FS service helpers: api/app/freeswitch_service.py:1; result hook: api/app/main.py:3406

## MCP Integration
- Transports listed and implemented — VERIFIED
  - Node stdio/http/sse/ws refs above; Python stdio/SSE refs above
- Limits: HTTP JSON 16 MB — VERIFIED: node_mcp/src/servers/http.js:15
- Stdio preferred for filePath — POLICY documented; acceptable

## SDKs
- Identical error mapping and health check — VERIFIED
  - Node: sdks/node/index.js:27
  - Python: sdks/python/faxbot/__init__.py:30
- Version 1.1.0 — VERIFIED

## Auth & API Keys
- Multi‑key auth with fbk_live_* tokens — VERIFIED
  - Create/list/delete/rotate: api/app/main.py:2142, 2155, 2161, 2174
  - Scope enforcement: api/app/main.py:447–470, 2653–2671
- Set REQUIRE_API_KEY for prod; bootstrap via env API_KEY — VERIFIED: api/app/config.py:45, api/app/main.py:420

## HIPAA vs Non‑HIPAA Config
- Strict settings available (HTTPS, HMAC, audit logs) — VERIFIED
  - Enforce HTTPS flag: api/app/config.py:46, used in health/diagnostics: api/app/main.py:1614
  - Audit hooks present: api/app/audit.py:1; audit_event usages across main
- Relaxed dev profile — VERIFIED via defaults (require_api_key optional, OAuth optional)

## Inbound Receiving
- Enable with INBOUND_ENABLED — VERIFIED: api/app/config.py:117
- Endpoints: list/get/pdf — VERIFIED: api/app/main.py:2673, 2693, 2704
- Cloud callbacks: Phaxio+Sinch — VERIFIED: api/app/main.py:2826, 2955
- Storage backends local|s3 (SSE‑KMS) — VERIFIED: api/app/storage.py:54–60
- Retention/TTL defaults — VERIFIED: api/app/config.py:125, 142–143
- Idempotency on (provider_sid, event_type) — VERIFIED: api/app/db.py:98
- UI coverage for inbound — PARTIAL
  - Inbound.tsx present; needs full PHI warnings/KMS hints review: api/admin_ui/src/components/Inbound.tsx:1

## Key API Endpoints & Workflows
- Core endpoints — VERIFIED
  - /fax send: api/app/main.py:1970; /fax/{id}: api/app/main.py:2133; /fax/{id}/pdf tokenized: api/app/main.py:2260
  - /phaxio‑callback: api/app/main.py:2315; /health: api/app/main.py:296
- Admin Console surface (must‑haves) — PARTIAL
  - Settings/Diagnostics/Jobs/Keys/Inbound/Plugins present; ensure every field has helper text + docs link consistently (spot‑check OK)

## Plugins (v3)
- Feature flags — VERIFIED: api/app/config.py:160–162; server checks: api/app/main.py:3339
- HTTP manifest runtime — VERIFIED: api/app/plugins/http_provider.py:105
- Admin endpoints validate/install manifests — VERIFIED: api/app/main.py:1086, 1112
- Security defaults (HIPAA) — PARTIAL
  - Allowed domains/timeouts implemented in runtime; redaction policy is TODO

## Security Architecture
- Threat model addressed by controls — VERIFIED/PARTIAL
  - No PHI in UI logs; UI masks numbers (JobsList) — VERIFIED: api/admin_ui/src/components/JobsList.tsx:262
  - Webhook verification — VERIFIED (Phaxio; optional Sinch HMAC/basic): api/app/main.py:2321, 2973, 2980
  - Local‑only Admin UI gate — VERIFIED: api/app/main.py:158–175
  - Terminal WS auth (env key or DB admin key) — VERIFIED: api/app/main.py:3445–3466
  - Note: Terminal WS itself is not IP‑restricted; guidance remains to keep Admin local only

## Testing Strategy & Validation
- Backend matrix present in docs — VERIFIED: docs/V3_PHASE_STATUS.md:19, docs/API_TESTS.md:1
- API tests exist for core + Phaxio — VERIFIED: api/tests/test_api.py:1, api/tests/test_phaxio.py:1
- MISSING tests for Admin Actions and Terminal WS (see TODO)

## Deployment Considerations
- Docker compose services/ports — VERIFIED: docker-compose.yml:3
- Production guidance mirrors docs — VERIFIED: docs/DEPLOYMENT.md:1

## Admin Console: Terminal
- UI component + WS endpoint — VERIFIED: api/admin_ui/src/components/Terminal.tsx:17; api/app/main.py:3442
- Local‑only and auth gating — VERIFIED/PARTIAL (UI gate is local‑only; WS relies on admin key)
- Docs page exists — VERIFIED: docs/TERMINAL.md:1

## Admin Console: Scripts & Tests
- Backend‑aware quick actions — PARTIAL
  - Present for Phaxio and SIP; MISSING for Sinch/SignalWire/FreeSWITCH: api/admin_ui/src/components/ScriptsTests.tsx:250
- Container checks allowlist — VERIFIED: api/app/main.py:1279; UI: api/admin_ui/src/components/ScriptsTests.tsx:281
- Concurrency lock between cards — VERIFIED: per‑card busy flags: api/admin_ui/src/components/ScriptsTests.tsx:55–57

## Docs & Linking
- Jekyll docs for scripts/tests and MCP — VERIFIED: docs/scripts-and-tests.md:9, docs/NODE_MCP_SCRIPTS.md:11
- All links derive from DOCS_BASE_URL in UI — VERIFIED: api/app/main.py:517; ui usage: api/admin_ui/src/App.tsx:452
- Remove Plugin Builder references from docs — VERIFIED (no mentions in docs search)

## Common Pitfalls
- Avoid backend mixing in UI/docs — PARTIAL (helpers still missing for some backends; mixing not observed)

## Final Reminders vs Code
- Phaxio implementation complete — VERIFIED
- AMI must not be public — VERIFIED guidance in docker-compose.yml:39, docs/DEPLOYMENT.md:1
- OAuth optional for non‑PHI — VERIFIED: api/app/config.py:151

---

## Notable Gaps (added to TODO.md)
- Scripts & Tests: add Sinch, SignalWire, FreeSWITCH helper cards and backend‑specific guidance in UI.
- Jobs detail: add backend‑aware troubleshooting links and docs anchors for failed jobs.
- Tests: add coverage for /admin/actions and /admin/terminal WS auth/behavior.
- Redaction policy for manifest providers (response/request snippets) to avoid secret leakage in Admin UI.
- Minor: Replace onKeyPress in App login with onKeyDown for broader browser compatibility.
- Optional: Add explicit local‑network guardrails note in Terminal UI and surface ENABLE_LOCAL_ADMIN state.

## Out‑of‑Repo Notes
- Vendor admin demo (faxbot.net) parity validated separately; not present in this repo. Ensure nav grouping, Scripts & Tests, Terminal seeding, and Plugins demo mirror current Admin Console.

