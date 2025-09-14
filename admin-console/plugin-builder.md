---
layout: default
title: Plugin Builder
parent: Admin Console
nav_order: 4
permalink: /admin-console/plugin-builder/
---

# Plugin Builder

Generate a starter outbound plugin (Python or Node) with a guided wizard.

Where
- Admin Console → Plugins → “Build Plugin” (preview)

What it creates
- Python class extending `FaxPlugin` with `send_fax`/`get_status`
- Node class extending `FaxPlugin` with `sendFax`/`getStatus`
- JSON manifest fields: id, name, version, categories, capabilities, config schema

How to use
1) Enter basic info (name, id, version)
2) Choose SDK (Python/Node)
3) Provider settings (e.g., SIP trunk label, require T.38)
4) Capabilities (send, get_status)
5) Review & Generate → download the source file

Next steps
- Place the generated plugin in your plugin workspace
- Implement transport‑specific logic (e.g., AMI call for SIP or custom cloud API)
- Keep secrets in environment; only non‑secret settings go into plugin config

References
- Plugins (v3): [Overview](/Faxbot/plugins/)

