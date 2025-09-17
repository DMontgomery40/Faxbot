---
layout: default
title: Desktop
parent: Apps
nav_order: 2
permalink: /apps/desktop
---

# Desktop App (Electron)

The Desktop app wraps the Admin Console in Electron for a native-feeling experience.

Highlights
- Same UI and features as the in-browser Admin Console.
- Local-only by default; respects Admin Console security (no PHI in logs, no external origins).
- Uses the same API endpoints as the web UI.

Notes
- Do not expose the Admin Console Terminal through public tunnels; it is intended for local-only administration.
- See the Admin Console docs for CSP and security details: {{ site.baseurl }}/local_admin_console

