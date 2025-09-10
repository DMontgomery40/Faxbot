---
layout: default
title: Admin Console
nav_order: 4
permalink: /admin-console/
---

# Admin Console

> The local Admin Console helps you manage keys, jobs, inbound inbox, diagnostics, and settings without editing `.env` files by hand.

- Local‑only by default; protect with API keys and loopback restrictions
- Works alongside any backend (Phaxio, Sinch, SIP/Asterisk)
- Provides copy‑ready configuration lines after validation

## Usage

- Access at `http://localhost:8080/admin/ui/` when the API is running
- Use an admin‑scoped API key to log in (or bootstrap key in development)
- Explore tabs for Dashboard, Send, Jobs, Inbound, Keys, Settings, Diagnostics

## Demo (Simulated)

- Hosted demo with simulated data: https://faxbot.net/admin-demo/
- No external calls; intended for showcasing the workflow

