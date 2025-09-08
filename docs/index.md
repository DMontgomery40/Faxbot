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
The first and only open-source, self-hostable fax API. Send faxes with a single function call.

Yes, this repo might look overwhelming at first glance—that's only because Faxbot supports multiple backends (cloud and self-hosted), several MCP transport options for AI integration, and HIPAA-compliant security configurations. Most users will only need one path through this complexity.

**Core function:** `send_fax(phone_number, pdf_file)` → Done.

To our knowledge, no other open-source project combines:

- Modern REST API for fax transmission
- Multiple backend options (Phaxio cloud, Sinch cloud, self-hosted SIP/Asterisk)
- AI assistant integration via MCP (Model Context Protocol)
- HIPAA compliance features for healthcare PHI
- Developer SDKs for Node.js and Python

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
  