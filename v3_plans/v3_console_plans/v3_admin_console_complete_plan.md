# v3 Admin Console — Complete Implementation Plan (Exhaustive)

Owner: Admin Console Team
Branch: development
Status: Living plan — GUI-first mandate, plugin-aware, HIPAA-aligned

Scope
- Eight detailed phases to deliver a complete, GUI-first Admin Console
- React + TypeScript (Vite) components, forms, and table designs
- Exact API endpoints and scopes used; no server drift from OpenAPI
- Breaking-change warnings and rollout checkpoints
- Agent working instructions with explicit "Stop for review" gates

Design Principles (non-negotiable)
- GUI-first: no capability without a corresponding UI path
- Backend isolation: show provider-specific guidance only for the selected backend
- Admin Console parity: feature is “done” only when usable from the console
- HIPAA-safe defaults: secure by default; docs explain tradeoffs for non-PHI users
- Contextual help everywhere: tooltips + Learn more links pointing to docs
- Mobile-first: test common breakpoints for readability and safe taps

Core Endpoints (must exist and match OpenAPI)
- Health: GET /health
- Outbound fax: POST /fax, GET /fax/{id}
- Inbound (feature-flag): GET /inbound, GET /inbound/{id}, GET /inbound/{id}/pdf
- Admin keys: POST /admin/api-keys, GET /admin/api-keys, DELETE /admin/api-keys/{keyId}, POST /admin/api-keys/{keyId}/rotate
- Plugins (feature-flag): GET /plugins, GET /plugins/{id}/config, PUT /plugins/{id}/config, GET /plugin-registry

Auth & Scopes
- Global: X-API-Key header required in production (REQUIRE_API_KEY=true)
- Scopes: fax:send, fax:read, inbound:list, inbound:read, keys:manage, admin:plugins:read, admin:plugins:write

Phase 1 — Foundation & Navigation (Week 1)
Goal: Lay down the console shell, routing, auth guard, and nav that reflects backends/plugins/inbound.

Components
- AppShell (layout, header, responsive nav)
- AuthGate (API key prompt with persistent storage; redacts key from logs)
- TopNav items: Dashboard, Send, Jobs, Inbound, Keys, Settings, Diagnostics, Plugins (feature-flag)

API Integration
- Bootstrap health check: GET /health
- Read feature flags from GET /health (extension) or presence of GET /plugins to toggle nav items

UX
- Show locked-state cards when feature flags disable pages (e.g., Plugins disabled)
- Add Help icon per page linking to focused docs (no mixed backends)

Stop for review
- Confirm nav order and feature-flag behavior
- Confirm mobile breakpoint behavior (iPhone 13/Pro Max & Pixel 5)

Phase 2 — Settings & Backend Selection (Week 1–2)
Goal: A single screen to select active outbound backend and set security defaults.

Components
- Settings/BackendCard (select: phaxio | sinch | sip | disabled)
- Settings/SecurityCard (REQUIRE_API_KEY, ENFORCE_PUBLIC_HTTPS, AUDIT_LOG_ENABLED)
- Settings/InboundCard (feature-flag): INBOUND_ENABLED, retention days, token TTL
- Settings/StorageCard: local vs S3, KMS id (tooltip), endpoint for S3-compatible

Forms
- Schema-driven config forms per provider (derived from /plugins/{id}/config where available)
- Client-side validation with crisp helper text

API Integration
- Read-only of current effective config via /plugins/{id}/config (when FEATURE_V3_PLUGINS=true)
- Persist strategy (v3): keep console writes gated to PUT /plugins/{id}/config and surface “apply during maintenance windows” banner

UX
- Tooltips on every field + Learn more link
- HIPAA banner if ENFORCE_PUBLIC_HTTPS=false and INBOUND_ENABLED=true

Stop for review
- Validate that only the selected provider’s guidance renders
- Verify S3/KMS help copy with docs team

Phase 3 — Keys (API Key Management) (Week 2)
Goal: End-to-end key lifecycle in UI.

Components
- Keys/List (name, owner, scopes, created, last used, actions)
- Keys/CreateModal (scopes multiselect, owner text, copy-once token)
- Keys/RotateModal, Keys/RevokeConfirm

API Integration
- POST /admin/api-keys returns token once (copy-once flow)
- GET /admin/api-keys, DELETE /admin/api-keys/{keyId}
- POST /admin/api-keys/{keyId}/rotate

UX
- On create/rotate: reveal fbk_live_<keyId>_<secret> once; encourage storing securely
- Copy-to-clipboard with masked display after copy

Stop for review
- Run smoke: create/rotate/revoke → success paths + error states with actionable messages

Phase 4 — Send Fax (Outbound) (Week 2–3)
Goal: Single-screen outbound send with preflight checks.

Components
- SendFax/Form (to number, file picker PDF/TXT only, backend hints)
- SendFax/Preflight (file size ≤ 10 MB, extension check, E.164 hint)
- SendFax/Result (job id, status link)

API Integration
- POST /fax multipart; display id & status
- GET /fax/{id} for status link

UX
- Reject non-PDF/TXT with inline helper
- If MCP users: hint stdio filePath vs HTTP/SSE JSON size limits (no base64 here)

Stop for review
- Validate with each backend’s minimal credentials (dev mode acceptable)

Phase 5 — Jobs (List & Detail) (Week 3)
Goal: View queue, search/filter, and targeted troubleshooting links.

Components
- Jobs/List (id, to, status, pages, backend, created, updated; filters: status, backend, date)
- Jobs/Detail (timeline, errors, PDF link for phaxio when available)

API Integration
- Use existing job list/status endpoints (if only status-by-id exists, list is server-derived or local-friendly demo)

UX
- On failure: show backend-specific “Troubleshooting” links (no cross-backend mixing)

Stop for review
- Confirm failure mapping per backend

Phase 6 — Inbound (Inbox) (Week 3–4)
Goal: Inbound toggle and secure PDF access.

Components
- Inbound/List (id, from, to, pages, status, received)
- Inbound/Detail (metadata, download link with short TTL or API-key gating)

API Integration
- GET /inbound, GET /inbound/{id}, GET /inbound/{id}/pdf?token=...
- Token TTL sourced from Settings; validate HMAC flags when using cloud callbacks

UX
- Clear PHI warnings; retention countdown; scoped download links

Stop for review
- Confirm storage backend configured (local vs S3) and PDF access pattern is HIPAA-safe

Phase 7 — Diagnostics (Week 4)
Goal: One-stop health and configuration checks.

Components
- Diagnostics/Checks (health, webhooks signature validation, storage checks, limits)
- Diagnostics/Actions (optional restart if ADMIN_ALLOW_RESTART=true)

API Integration
- GET /health + optional extended diagnostics endpoint (non-sensitive)

UX
- Present actionable remediation (with links) rather than raw codes

Stop for review
- Validate edge cases (missing env, misconfigured S3/KMS, invalid webhook secrets)

Phase 8 — Plugins Tab (Feature-Flag) (Week 4–5)
Goal: Read-only plugin discovery + config persistence.

Components
- Plugins/List (installed plugins, manifests, enabled/configured state)
- Plugins/ConfigForm (JSON Schema-driven forms per plugin)

API Integration
- GET /plugins, GET /plugins/{id}/config, PUT /plugins/{id}/config
- GET /plugin-registry (curated registry; install disabled by default)

UX
- “Apply during maintenance” banner; rollback note if invalid config
- No remote installs by default; HIPAA profiles keep install disabled

Stop for review
- Validate JSON Schema form constraints; verify no secrets echoed in network panel

React/TypeScript Component Notes
- Form: react-hook-form + zod or JSON-Schema-based form
- Table: headless table with virtualization; clear truncation for small screens
- State: prefer SWR/React Query per page; avoid global mutation for secrets
- Error handling: map 400/401/404/413/415 to friendly messages per AGENTS.md

Breaking-Change Warnings
- Do not drift from OpenAPI — SDKs and UI share contracts
- Never log PHI in UI console; mask numbers in UI and logs
- Keep /admin/api-keys returning token once only — UI assumes copy-once
- Plugins: never show cross-backend instructions; isolation is enforced at the UI layer

Rollout & Checkpoints
- Phase-by-phase demo recordings and screenshots
- Smoke tests for Keys, Send, Jobs status, and Diagnostics
- Mobile review pass after Phase 5
- HIPAA copy/legal review before public docs changes

Agent Working Instructions
- Stop for review gates at the end of each phase above
- If a server gap blocks progress, document it and park the UI work with mocked data until the API is updated
- Keep copy concise in the UI; link to docs for deep detail

Links (docs)
- Admin Console: /admin-console/
- Backends: /backends/
- Plugins: /plugins/
- Security: /security/

