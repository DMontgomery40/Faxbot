---
layout: default
title: Home
nav_order: 1
description: "The first open-source, self-hostable fax API with AI integration"
permalink: /
---

# Faxbot

<div style="text-align:center;margin:16px 0;">
  <img src="{{ site.baseurl }}/docs/assets/images/faxbot_full_logo.png" alt="Faxbot logo" style="max-width:100%;width:100%;height:auto;" />
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