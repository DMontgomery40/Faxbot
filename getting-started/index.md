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

Faxbot is Admin Console first. Use this short path to bring up the API and walk through the Setup Wizard—no manual config files or CLI tooling required beyond Docker Compose.

## Launch Faxbot

1. Copy `.env.example` to `.env` (you can edit later in the UI).
2. Start the API: `docker compose up -d --build api`
3. Open the Admin Console at `http://localhost:8080/admin/ui/`.

If the Admin Console returns 404, enable the local UI mount and restart the API:

- Add `ENABLE_LOCAL_ADMIN=true` to `.env` (preferred on current builds), or
- Set `ADMIN_UI_DIR=/app/admin_ui_dist` (some images auto‑mount this path)

Then run: `docker compose up -d --build api`

{: .note }
Need a quick tour first? See the [Admin Console]({{ site.baseurl }}/admin-console/) page and the hosted [Admin Demo](https://faxbot.net/admin-demo/).

## Complete the Setup Wizard

1. In the console, open **Setup Wizard**.
2. Choose your outbound provider (Phaxio, Sinch, SIP/Asterisk, SignalWire, or Test Mode).
3. Enter credentials and security preferences (helper text and “Learn more” links guide each field).
4. Apply settings. The API reloads, and your backend is ready.

That’s it for onboarding. The provider guides cover details when you’re ready.

## What to do next

- Follow the provider guides under [Backends]({{ site.baseurl }}/backends/) for credentials, networking, and HIPAA notes.
- Manage keys, storage, inbound receiving, and diagnostics from the Admin Console tabs (each screen links to matching docs).
- Integrate your app using the [Node]({{ site.baseurl }}/development/node-sdk.html) or [Python]({{ site.baseurl }}/development/python-sdk.html) SDK once outbound faxing is verified.

## Need help?

Open an issue or see [Contributing](contributing.html) for support options. Mention which backend you’re using so we can point you to the right playbook.

## Under the Hood (for developers)

- The Setup Wizard calls Admin APIs to validate credentials, apply settings, and optionally persist a server‑side `.env`:
  - `POST /admin/settings/validate` — backend‑specific checks
  - `PUT /admin/settings` — in‑process apply via environment
  - `POST /admin/settings/persist` — save `.env` to `/faxdata/faxbot.env` when enabled
- Outbound sending uses the selected backend:
  - Cloud (Phaxio/Sinch/SignalWire/Documo): upload or tokenized fetch of your PDF
  - SIP/Asterisk: PDF→TIFF conversion then AMI originate
- Inbound (optional): cloud callbacks (HMAC/Basic) or Asterisk internal post to `/_internal/asterisk/inbound`

See the full API surface: {{ site.baseurl }}/development/api-reference.html
