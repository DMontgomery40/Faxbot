---
layout: default
title: Curated Registry
parent: Plugins
nav_order: 2
permalink: /plugins/registry.html
---

# Curated Plugin Registry

When `FEATURE_V3_PLUGINS=true`, the API exposes discovery endpoints used by the Admin Console.

Endpoints (admin scope required)
- `GET /plugins` — list installed plugins with manifests and current enabled/config values
- `GET /plugins/{id}/config` — return `enabled` and `settings` for a plugin
- `PUT /plugins/{id}/config` — validate/persist via the JSON config store
- `GET /plugin-registry` — serve the curated registry JSON for UI search

Config store
- Resolved config path: `FAXBOT_CONFIG_PATH` (default `config/faxbot.config.json`)
- Atomic writes with `.bak` backups; roll back to last known‑good on validation/startup failure

Security & permissions
- New admin scopes: `admin:plugins:read`, `admin:plugins:write`
- Only keys with `keys:manage` may change plugin configs
- Per‑key RPM: mirror inbound list/get defaults for reads (stricter for writes)

Dynamic install (optional)
- Keep `FEATURE_PLUGIN_INSTALL=false` by default
- If enabled, use a strict allowlist and checksums (signatures if provided); non‑interactive, sandboxed install only
- For HIPAA profiles, leave remote install disabled

Admin Console behavior
- The Plugins tab reads `/plugins` and renders schema‑driven forms
- Only the active outbound provider’s help is shown; switching providers is a guided flow (no mixed instructions)

Notes
- Backends remain isolated across docs and UI; Phaxio users never see SIP/Asterisk instructions
- Inbound cloud callbacks remain core HTTP endpoints that delegate to plugin handlers; HMAC/signature verification is enforced in core

Troubleshooting
- If `/plugins` returns 404, enable the feature flag (`FEATURE_V3_PLUGINS=true`) and restart the API
- For config write errors, check file permissions on `FAXBOT_CONFIG_PATH` (the default lives under the `faxdata` volume)
