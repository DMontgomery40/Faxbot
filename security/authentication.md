---
layout: default
title: Authentication (API Keys)
parent: Security
nav_order: 1
permalink: /security/authentication/
---

# Authentication (API Keys)

Header
- Send `X-API-Key: <token>` on every request when enabled.

Enable
- In Admin Console → Settings → Security, set `API_KEY`.
- Or via environment: `API_KEY=...`.

Scopes (roadmap alignment)
- Outbound: `fax:send`, `fax:read`
- Inbound: `inbound:list`, `inbound:read`
- Admin: `keys:manage`, `admin:plugins:read`, `admin:plugins:write`

Tips
- Require API keys in production; use different values for dev vs prod.
- Combine with reverse‑proxy rate limits for defense‑in‑depth.

