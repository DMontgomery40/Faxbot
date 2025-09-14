---
layout: default
title: Getting Started
nav_order: 2
has_children: true
permalink: /getting-started/
---

<div class="home-hero">
  <img src="{{ site.baseurl }}/assets/images/faxbot_full_logo.png" alt="Faxbot logo" />
</div>

# Getting Started

Welcome to Faxbot! This section will help you get up and running quickly.

## What is Faxbot?

Faxbot is the first and only open-source, self-hostable fax API that combines:
- Simple REST API for sending faxes
- Multiple backend options (cloud and self-hosted)
- AI assistant integration via MCP
- HIPAA compliance features
- Developer SDKs for Node.js and Python

{: .highlight }
Need HIPAA? Use Phaxio or SSE+OAuth. For local dev, you can disable faxing with `FAX_DISABLED=true`.

## Choose Your Path

- **Phaxio (Cloud, recommended):** start here for a 5‑minute setup.
  - [Phaxio Setup Guide](/Faxbot/backends/phaxio-setup.html)
- **Sinch Fax API v3 (Cloud):** direct upload flow for “Phaxio by Sinch” accounts.
  - [Sinch Setup Guide](/Faxbot/backends/sinch-setup.html)
- **SIP/Asterisk (Self‑Hosted):** full control, no per‑fax cloud charges.
  - [SIP/Asterisk Setup Guide](/Faxbot/backends/sip-setup.html)

### Receiving

- When enabled, received faxes appear in the Admin Console inbox and are accessible via the API. See the [API Reference](/Faxbot/development/api-reference.html).

After the API is running, optionally add AI assistant control:
- [MCP Integration](/Faxbot/ai-integration/mcp-integration.html)

## Quick Checks

- `GET /health` returns `{ "status": "ok" }` when the API is up
- `X-API-Key` header is required if `API_KEY` is set
- Max upload size defaults to 10 MB (configurable)

## Next Steps

- Review [Security](/Faxbot/security/) if handling PHI
- Explore the [Admin Console](/Faxbot/admin-console/) for keys, jobs, inbound inbox, diagnostics, and settings
- Try the [SDKs](/Faxbot/development/sdks.html) to integrate quickly

## Need Help?

Don't hesitate to ask questions! See our [Contributing guide](contributing.html) for the best way to get help.
