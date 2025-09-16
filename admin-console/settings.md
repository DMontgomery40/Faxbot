---
layout: default
title: Settings
parent: Admin Console
nav_order: 3
permalink: /admin-console/settings/
---

# Settings

All runtime configuration lives here once the Setup Wizard has been completed. Every field includes inline helper text and a **Learn more** link that deep-links to the matching docs page.

## Backend tab

- Pick the active outbound provider or swap to Test Mode without touching `.env`
- Each provider reveals only the fields it needs (Phaxio keys, Sinch project ID, SIP trunk credentials, etc.)
- Security profiles (HIPAA vs Non-PHI) control defaults like HTTPS enforcement and signature verification
- Applying changes regenerates the plugin config and restarts the API in-place

## Security tab

- Require REST API keys and mint them on the **API Keys** page
- Enforce HTTPS for callback URLs, toggle audit logging, and set rate limits
- Adjust max upload size and allowed file types; warnings surface in Send Fax when values are restrictive

## Storage & Inbound tab

- Select local storage (dev only) or S3/S3-compatible with optional SSE-KMS
- Configure inbound fax retention windows, download token TTL, and per-scope rate limits
- Warnings surface when HIPAA defaults are relaxed so you can document exceptions

## MCP tab

- Enable/disable Node and Python MCP transports (stdio, HTTP, SSE)
- Provide OAuth issuer/audience for SSE when handling PHI
- Copy/paste starter configs for Claude Desktop, Cursor, and Windsurf

## Export & versioning

- Download the resolved config for change control
- Reapply the last known-good configuration if a test change causes an outage

Need step-by-step provider help? Jump to [Backends]({{ site.baseurl }}/backends/) for detailed walkthroughs tailored to each option.
