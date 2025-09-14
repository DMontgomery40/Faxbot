# Faxbot v3 — Phase 1 & 2 Status

Updated: 2025-09-13

## Completed (Safe, feature‑gated)
- New settings in `api/app/config.py`:
  - `FEATURE_V3_PLUGINS` (default false)
  - `FAXBOT_CONFIG_PATH` (default `config/faxbot.config.json`)
  - `FEATURE_PLUGIN_INSTALL` (default false)
- Plugin scaffolding (not imported unless feature enabled):
  - `api/app/plugins/base/types.py`
  - `api/app/plugins/config_store.py` (atomic JSON read/write)
- Admin API (behind `FEATURE_V3_PLUGINS` and `require_admin`):
  - `GET /plugins` — list built‑in providers as plugin manifests
  - `GET /plugins/{id}/config` — return effective config (sanitized)
  - `PUT /plugins/{id}/config` — persist to JSON only (no live apply)
  - `GET /plugin-registry` — curated registry (reads `config/plugin_registry.json`)
- Curated registry file: `config/plugin_registry.json`
- Admin Console (feature‑gated):
  - Plugins tab renders when `FEATURE_V3_PLUGINS=true` and lists provider plugins; supports persisting outbound selection to config file (no live apply).

## Deferred / Sketchy
- Live application of JSON plugin config (overriding env)
- New admin scopes (`admin:plugins:read|write`) and UI support
- Dynamic plugin install (`FEATURE_PLUGIN_INSTALL=true`)
- WebSocket transport for MCP (non‑goal for v3 per AGENTS.md)

## Notes
- All new behavior is disabled by default and does not change existing backend selection (`FAX_BACKEND`).
- These foundations unblock Admin Console work on a Plugins tab without risking production behavior.
