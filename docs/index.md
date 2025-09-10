---
layout: default
title: Home
nav_order: 1
description: "The first open-source, self-hostable fax API with AI integration"
permalink: /
---


<div class="home-hero">
  <img src="{{ site.baseurl }}/docs/assets/images/faxbot_full_logo.png" alt="Faxbot logo" />
</div>

{: .highlight }
The first and only open-source, self-hostable fax API. Send and receive faxes with a single function call. Operate everything via a local Admin Console.

Yes, this repo might look overwhelming at first glance—that's only because Faxbot supports multiple backends (cloud and self-hosted), several MCP transport options for AI integration, and HIPAA-compliant security configurations. Most users will only need one path through this complexity.

**Core functions:**
- `send_fax(phone_number, pdf_file)` → Outbound
- `GET /inbound` + `GET /inbound/{id}/pdf` → Inbound

### Why Faxbot

- Open source and self‑hostable end‑to‑end: run it entirely on your infra, modify as needed.
- One API, many backends: switch Phaxio ↔ Sinch ↔ self‑hosted SIP/Asterisk via environment settings.
- Bring‑your‑own SIP trunk: choose any SIP provider; migrate later by changing a couple of env vars.
- Fully local option: when using SIP, no third‑party cloud in the path; artifacts stay on your storage (S3/MinIO supported).
- AI assistant tools built‑in: MCP servers (Node & Python) for stdio/HTTP/SSE; desktop stdio supports `filePath` (no base64 size pain).
- Inbound receiving: cloud webhooks with signature verification or Asterisk ReceiveFAX → TIFF→PDF, mailbox routing, short‑TTL tokens, retention windows.
- Test/dev backend: simulate send/receive flows without hitting a paid provider.
- Vendor‑neutral SDKs: identical Node/Python clients so your app code is portable.

---

## Admin Console

Faxbot includes a local Admin Console for keys, jobs, inbound inbox, diagnostics, and settings.

- Guide: [Local Admin Console]({{ site.baseurl }}/LOCAL_ADMIN_CONSOLE.html)
- Demo UI: https://faxbot.net/admin-demo/ (simulated data)

## Receiving Faxes (Inbound)

When enabled, inbound faxes appear in the Inbox and via API:

- `GET /inbound` → list most recent inbound faxes (metadata)
- `GET /inbound/{id}/pdf` → short‑lived PDF download (tokenized, TTL configurable)

Configuration depends on backend:
- Cloud (Phaxio/Sinch): Configure webhooks to point to your instance; enable signature verification
- SIP/Asterisk: Use ReceiveFAX → convert TIFF→PDF; route to storage; enforce retention policy

See: [Inbound Overview]({{ site.baseurl }}/inbound/)

Questions? Issues? Please don't hesitate to reach out. See `CONTRIBUTING.md` for the best way to get help.

---

## Table of Contents

- Getting Started
  - [Overview]({{ site.baseurl }}/getting-started/)
- Backends
  - [Phaxio Setup]({{ site.baseurl }}/backends/phaxio-setup.html)
  - [Sinch Setup]({{ site.baseurl }}/backends/sinch-setup.html)
  - [SIP/Asterisk Setup]({{ site.baseurl }}/backends/sip-setup.html)
  - [Images & PDFs]({{ site.baseurl }}/backends/images-and-pdfs.html)
- AI Integration
  - [Overview]({{ site.baseurl }}/ai-integration/)
  - [MCP Integration]({{ site.baseurl }}/ai-integration/mcp-integration.html)
  - [Node MCP]({{ site.baseurl }}/ai-integration/node-mcp.html)
- Security
  - [Overview]({{ site.baseurl }}/security/)
  - [HIPAA Requirements]({{ site.baseurl }}/security/hipaa-requirements.html)
  - [OAuth Setup]({{ site.baseurl }}/security/oauth-setup.html)
- Development
  - [Overview]({{ site.baseurl }}/development/)
  - [API Reference]({{ site.baseurl }}/development/api-reference.html)
  - [Client SDKs]({{ site.baseurl }}/development/sdks.html)
  - [Troubleshooting]({{ site.baseurl }}/development/troubleshooting.html)
  - [Phaxio E2E Test]({{ site.baseurl }}/development/phaxio-e2e-test.html)
  - [Node.js SDK]({{ site.baseurl }}/development/node-sdk.html)
  - [Python SDK]({{ site.baseurl }}/development/python-sdk.html)
  - [Changelog]({{ site.baseurl }}/development/changelog.html)

---

## Enterprise Services

Need custom integrations with platforms like Spruce and other healthcare communication tools, or a managed deployment with BAAs? We provide enterprise services while keeping Faxbot open‑source and self‑hostable.

- Learn more: https://faxbot.net/compliance
- Contact: mailto:david@faxbot.com
  
