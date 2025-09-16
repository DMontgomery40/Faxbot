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

Faxbot is now Admin Console first. Use this short path to get a local instance online and walk through the Setup Wizard—no manual config files required.

## Launch Faxbot

1. Copy `.env.example` to `.env` (only change values if you already know your backend).
2. Start the stack: `docker compose up -d --build api`
3. Open the Admin Console at http://localhost:8080 and sign in with the default Admin Console password from `.env` (or set `ADMIN_PASSWORD` before starting).

{: .note }
Need a hands-off demo? Skip straight to the [Admin Console Demo]({{ site.baseurl }}/admin-console/).

## Complete the Setup Wizard

1. In the console, click **Setup Wizard**.
2. Choose your outbound provider (Phaxio, Sinch, SIP/Asterisk, SignalWire, or Test Mode).
3. Enter credentials and security preferences using the inline guidance on each step.
4. Review and apply. Faxbot writes the config file and restarts the API for you.

That is the entire onboarding flow. The rest of the docs dive into provider-specific nuances when you are ready.

## What to do next

- Follow the tailored provider guides under [Backends]({{ site.baseurl }}/backends/) for credentials, networking, and HIPAA notes.
- Manage keys, storage, inbound receiving, and diagnostics from the Admin Console tabs (each screen links to matching docs for deeper context).
- Integrate your app using the [Node]({{ site.baseurl }}/development/node-sdk.html) or [Python]({{ site.baseurl }}/development/python-sdk.html) SDK once outbound faxing is verified.

## Need help?

Open an issue or see [Contributing](contributing.html) for support options. Let us know which backend you picked so we can respond with the right playbook.
