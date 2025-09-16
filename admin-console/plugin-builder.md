---
layout: default
title: Plugin Builder
parent: Admin Console
nav_order: 5
permalink: /admin-console/plugin-builder/
---

# Plugin Builder

Generate a starter outbound plugin with a guided wizard. The builder supports two tracks:
- Code plugins (Python/Node)
- HTTP Manifest providers

Where
- Admin Console → Plugins → “Build Plugin” (when enabled)

What it creates
- Python plugin: a class extending `FaxPlugin` with `send_fax` and optional `get_status`
  - Files: `providers/<id>/__init__.py`, `providers/<id>/plugin.py`
  - Registration snippet for the runtime
- Node plugin: a class extending `FaxPlugin` with `sendFax`/`getStatus`
  - Files: `providers/<id>/index.js` (or TS source), package metadata
- HTTP Manifest: `config/providers/<id>/manifest.json` with `actions.send_fax` and optional `get_status`
  - Allowed domains, auth scheme, headers, body templates, response mapping

How to use
1) Enter basics (name, id, version)
2) Choose target (Python, Node, or HTTP Manifest)
3) Configure settings schema (non‑secrets) and capability set (send, get_status)
4) Review & Generate → download the source or manifest
5) Place files in your workspace and enable via Admin Console → Plugins

How it works (under the hood)
- Manifest providers are executed by the runtime in `api/app/plugins/http_provider.py`
- The Console persists plugin enablement/settings via `PUT /plugins/{id}/config` using the config store in `api/app/plugins/config_store.py`
- When a manifest provider is active, outbound send dispatches through the manifest runtime (see `api/app/main.py` for dispatch and discovery endpoints)

Security model
- Secrets are provided via environment/Admin settings; manifests store no secrets
- Each manifest must include `allowed_domains`; the runtime blocks other hosts
- Admin writes require `admin:plugins:write`; reads require `admin:plugins:read`

Developer tips
- Keep provider‑specific docs separate; do not leak instructions across providers
- Add “Learn more” links for operators that point to minimal, focused pages
- For HIPAA, keep remote install disabled and commit manifests to source control

References
- Plugins (v3): [Overview]({{ site.baseurl }}/plugins/)
- Curated Registry: [Docs]({{ site.baseurl }}/plugins/registry.html)
- HTTP Manifest Providers: [Docs]({{ site.baseurl }}/plugins/manifest-http.html)
