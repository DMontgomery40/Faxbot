---
layout: default
title: Plugins
nav_order: 5
has_children: true
permalink: /plugins/
---

# Plugins

Faxbot v3 introduces runtime‑switchable provider plugins backed by a single resolved config file. Initially supported slots are outbound (send faxes) and storage (inbound artifact storage). Inbound cloud callbacks remain core endpoints that delegate to plugin‑specific handlers; signature verification stays in core.

- Outbound: exactly one active provider (e.g., phaxio, sinch, sip, documo, signalwire, or an HTTP manifest provider)
- Storage: local or S3/S3‑compatible (MinIO) for inbound PDFs

Feature flags and config
- Enable discovery endpoints: set `FEATURE_V3_PLUGINS=true` on the API
- Optional dynamic install (off by default): `FEATURE_PLUGIN_INSTALL=true` (strict allowlist & checksums required)
- Resolved config file path: `FAXBOT_CONFIG_PATH` (default `config/faxbot.config.json`)

Admin Console integration
- Plugins tab lists installed providers with enable/disable toggles
- Each provider shows schema‑driven settings and “Learn more” links
- UI surfaces only the currently selected outbound provider’s guidance (backend isolation)

Learn more
- [Curated Registry]({{ site.baseurl }}/plugins/registry.html)
- [HTTP Manifest Providers]({{ site.baseurl }}/plugins/manifest-http.html)
