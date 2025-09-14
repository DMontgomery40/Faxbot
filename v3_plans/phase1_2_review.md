# Faxbot v3 Plans — Phase 1 & 2 Review

Last updated: 2025-09-13

Scope: Thorough critique of `v3_plan.md` and `v3_implementation_guide.md` focused on Phase 1 (reality check + base scaffolding) and Phase 2 (config store / discovery), with concrete mapping to the actual codebase.

## Summary

- Phase 1 (Reality Check + base scaffolding): Generally sound, but some steps assume a plugin framework that does not yet exist in code. Safe subset implemented: feature‑gated plugin types and atomic config store with minimal admin endpoints (disabled by default).
- Phase 2 (Config Store & discovery): The guide’s atomic config store aligns with HIPAA constraints; however, wiring it into runtime selection must not override env‑based safety semantics. Implemented: read/write config helpers and read‑only discovery endpoints behind `FEATURE_V3_PLUGINS`.

## What’s In The Code Today (Reality)

- Backend selection via env: `FAX_BACKEND` in `api/app/config.py` with `phaxio|sinch|sip`.
- Providers: `phaxio_service.py`, `sinch_service.py`, and SIP via `ami.py` plus `conversion.py`.
- Inbound: Core endpoints in `main.py` (`/_internal/asterisk/inbound`, `/phaxio-inbound`, `/sinch-inbound`), storage via `storage.py` (local|s3) with SSE‑KMS support.
- Auth: Multi‑key system with scopes in `auth.py`; admin endpoints gated via `require_admin` (env API_KEY or a key with `keys:manage`).
- Admin Console: React app under `api/admin_ui` consuming `/admin/*` endpoints. No Plugins tab yet.
- MCP: Node and Python MCP servers exist; main API mounts Python MCP for SSE/HTTP when enabled.

## Phase 1 — Critique & Alignment

Strengths:
- Reality check tasks exactly match current code layout (good file targets).
- HIPAA posture is respected: tokenized PDF access, HMAC verification, audit logger.

Gaps / Risks:
- The doc assumes immediate creation of a generalized plugin base and DI; this is safe only if isolated behind a feature flag and not imported by the core runtime.
- Admin scope model in the plan mentions `admin:plugins:*`; current code uses `require_admin` (bootstrap key or `keys:manage`). Introducing new scopes requires DB migrations and UI changes — defer for Phase 3.

Decisions (safe subset implemented):
- Add `api/app/plugins/base/types.py` and `api/app/plugins/config_store.py` — not imported unless the feature is enabled.
- Add new settings: `FEATURE_V3_PLUGINS`, `FAXBOT_CONFIG_PATH`, `FEATURE_PLUGIN_INSTALL` (all default off).

## Phase 2 — Critique & Alignment

Strengths:
- Atomic JSON config store with backups is appropriate; avoids partial writes and allows rollback.
- Discovery endpoints (`GET /plugins`, `GET /plugins/{id}/config`, `GET /plugin-registry`) are useful foundations for the Admin Console.

Gaps / Risks:
- The guide suggests environment variable migration and hot reload. Current `settings` has a “persisted env file” mechanism — JSON config must not unexpectedly override env unless explicitly enabled and communicated in Admin UI. Runtime application of plugin config is risky and should be deferred until proper validation and UI flows exist.
- The v3 plan elsewhere mentions WebSocket transport; the canonical architecture in AGENTS.md specifies stdio/HTTP/SSE for MCP, not WebSocket. Treat WebSocket as out‑of‑scope for Phases 1–2.

Decisions (safe subset implemented):
- Implement read/write config helpers and guarded minimal endpoints in `main.py` behind `FEATURE_V3_PLUGINS`.
- Endpoints surface current effective config from env; PUT persists to JSON only (no runtime apply yet).
- Added `config/plugin_registry.json` for curated discovery; endpoint falls back to built‑ins.

## Conflicts or Inconsistencies in the Docs

- v3_plan.md has two different “Phase 2” meanings: earlier as “Document reality vs plan” and later as “Core Transport Implementation (WebSocket)”. The latter conflicts with AGENTS.md (SSE/HTTP only) and should be reclassified to a later phase if ever needed.
- v3_implementation_guide.md assumes plugin admin scopes (`admin:plugins:*`) that don’t exist yet. Current pattern uses `require_admin`; scope expansion should be deferred.

## What’s Safe vs Sketchy for Phases 1–2

Safe (done):
- Add feature‑gated plugin scaffolding and atomic config store module (not imported unless enabled).
- Add guarded admin endpoints for discovery and config persistence (no live reconfiguration).
- Add curated plugin registry file and default fallback.

Sketchy (documented in separate file):
- Runtime application of JSON config overriding env without an explicit Admin UI flow and restart semantics.
- Dynamic plugin installation/execution.
- New admin scopes with migrations (`admin:plugins:read`, `admin:plugins:write`).
- Any WebSocket addition for MCP.

## Next Steps (Recommended)

- Admin Console: add a Plugins tab gated by `FEATURE_V3_PLUGINS`, consuming the new endpoints and providing clear “preview” labeling and help links.
- Validation: define JSON Schemas per plugin before allowing writes that affect runtime.
- Application: implement explicit “apply config” with restart or hot‑reload semantics, including clear warnings for HIPAA users.

