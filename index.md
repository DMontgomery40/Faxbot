---
layout: default
title: Home
nav_order: 1
description: "The first open-source, self-hostable fax API with AI integration"
permalink: /
---


<div class="home-hero">
  <img src="{{ site.baseurl }}/assets/images/faxbot_full_logo.png" alt="Faxbot logo" />
</div>

{: .highlight }
The first and only open‑source, self‑hostable fax API with a complete Admin Console. Send faxes in minutes without touching the command line.

Most users follow one simple path: start the container, open the Admin Console, complete the Setup Wizard, and send a test fax.

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

Manage everything from the Admin Console—backend selection, keys, jobs, inbound inbox, diagnostics, and settings.

- Guide: [Admin Console]({{ site.baseurl }}/admin-console/)
- Demo: [Admin Demo](https://faxbot.net/admin-demo/) (simulated data)



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
- Plugins
  - [Overview]({{ site.baseurl }}/plugins/)
  - [Curated Registry]({{ site.baseurl }}/plugins/registry.html)
  - [HTTP Manifest Providers]({{ site.baseurl }}/plugins/manifest-http.html)
- AI Integration
  - [Overview]({{ site.baseurl }}/ai-integration/)
- Security
  - [Overview]({{ site.baseurl }}/security/)
  - [HIPAA Requirements]({{ site.baseurl }}/security/hipaa-requirements.html)
  - [OAuth Setup]({{ site.baseurl }}/security/oauth-setup.html)
- Development
  - [Overview]({{ site.baseurl }}/development/)
  - [API Reference]({{ site.baseurl }}/development/api-reference.html)
  - [SDKs]({{ site.baseurl }}/development/sdks.html)
  - [Troubleshooting]({{ site.baseurl }}/development/troubleshooting.html)
  - [Changelog]({{ site.baseurl }}/development/changelog.html)

---

## Enterprise Services

Need custom integrations (Spruce, EHRs), managed hosting with BAAs, or help with audits? We provide enterprise services while keeping Faxbot open‑source and self‑hostable.

- Learn more: https://faxbot.net/compliance
- Contact: mailto:david@faxbot.com
  
