---
layout: default
title: Resolved Config File
parent: Plugins
nav_order: 4
permalink: /plugins/config-file.html
---

# Resolved Config File (v3)

Faxbot persists plugin enablement and settings to a single JSON file. This file is read/written atomically by the Admin API when `FEATURE_V3_PLUGINS=true`.

Default path
- `FAXBOT_CONFIG_PATH` (default `config/faxbot.config.json`)

Structure
```json
{
  "version": 1,
  "providers": {
    "outbound": { "plugin": "phaxio", "enabled": true, "settings": {} },
    "inbound": { "plugin": null, "enabled": false, "settings": {} },
    "auth":    { "plugin": null, "enabled": false, "settings": {} },
    "storage": { "plugin": "local", "enabled": true, "settings": {} }
  }
}
```

Writes
- The server writes via a temporary file + rename and keeps a `.bak` backup of the previous version
- If a write fails validation, the server returns an error and preserves the previous file

Admin endpoints
- `GET /plugins/{id}/config` — Shows current `enabled` + `settings`
- `PUT /plugins/{id}/config` — Persists changes to the config file

Notes
- The running backend is controlled by environment at process start; persisting a different outbound provider updates the config file for next restart
- Use the Admin Console to apply in‑process changes when safe; use persistence for long‑term settings
