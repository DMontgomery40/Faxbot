---
layout: default
title: Setup Wizard (Beginner Friendly)
parent: Admin Console
nav_order: 2
permalink: /admin-console/setup-wizard/
---

# Setup Wizard

New to fax? Start here. This wizard gets you sending in minutes and explains each choice in plain language, with direct links to the third‑party pages you’ll need.

What you’re choosing
- Phaxio by Sinch (cloud): A developer fax API (Phaxio is part of Sinch). Simple and reliable. Two flavors in Faxbot:
  - “Phaxio” flow: Phaxio fetches your PDF from your server via a public URL. Best if you already have a domain or can run a tunnel. Supports webhooks to update status.
  - “Sinch (Direct Upload)”: Faxbot uploads the PDF directly to Sinch. Best if you do not have a public URL or domain yet. Send-only works without webhooks.
- Documo mFax (cloud): Direct upload to Documo. No domain/tunnel required. Good beginner option alongside Sinch.
- SIP / Asterisk (self‑hosted): No third‑party cloud, uses your SIP trunk. More advanced; skip for now if you’re just getting started.

Do I need a domain?
- No, if you pick “Sinch (Direct Upload)”. You can send faxes without any public URL.
- Yes (or a tunnel) if you pick “Phaxio” and want status callbacks or if Phaxio must fetch your PDF from your server.
- You can always add a domain later. For quick tests, use the included tunnel script: `scripts/setup-phaxio-tunnel.sh`.

Beginner 5‑minute path (no domain required)
1) Choose backend: “Sinch (Direct Upload)”.
2) Create a Sinch/Phaxio account:
   - Sign up: https://dashboard.sinch.com/signup (this is where Phaxio signups redirect — that’s normal).
   - After signup, create a project, then get your Project ID and API key/secret.
     - Help: Sinch Fax docs → https://developers.sinch.com/docs/fax/overview/
3) In the wizard, paste:
   - Project ID → `SINCH_PROJECT_ID`
   - API key → `SINCH_API_KEY`
   - API secret → `SINCH_API_SECRET`
4) Click “Apply”. Then send a test fax in the Admin Console or via curl.

Alternative: Documo (no domain required)
1) Choose backend: “Documo (mFax)”.
2) Create an account and API key:
   - Sign up: https://www.mfax.io/pricing
   - Enable API and create an API key in the Documo web app.
   - Docs: https://docs.documo.com
3) In the wizard, paste:
   - API key → `DOCUMO_API_KEY`
   - Optional: enable sandbox → `DOCUMO_SANDBOX=true`
4) Click “Apply”, then send a test fax. No public URL is required for sending.


If you have a domain (or can run a temporary tunnel)
1) Choose backend: “Phaxio (Recommended)”.
2) Create an account and get credentials:
   - Phaxio site: https://www.phaxio.com → Sign Up takes you to Sinch (expected).
   - Direct signup: https://dashboard.sinch.com/signup
   - Get: `PHAXIO_API_KEY` and `PHAXIO_API_SECRET` (same as Sinch API key/secret in many accounts).
   - Official Phaxio docs: https://www.phaxio.com/docs/
3) Make your API reachable over HTTPS:
   - Option A: use your domain, e.g. `https://api.yourdomain.com`
   - Option B: quick test tunnel → run `scripts/setup-phaxio-tunnel.sh` to get a temporary `https://...trycloudflare.com` URL.
4) In the wizard, set:
   - `PUBLIC_API_URL` to your HTTPS URL
   - `PHAXIO_CALLBACK_URL` to `https://YOUR_URL/phaxio-callback`
   - Paste your API key/secret
5) Click “Apply”, then send a test fax. Status updates arrive via callback (HMAC verified by default).

Security profile
- HIPAA (strict): requires API key for your Faxbot API, enforces HTTPS, enables audit logging, verifies provider signatures.
- Non‑PHI (convenience): relaxed defaults for local/dev; you can switch to HIPAA later without changing providers.

What is Phaxio? What is Sinch?
- Phaxio is a developer‑focused fax API. It’s part of Sinch now. When you click “Sign Up” on phaxio.com, you’ll be redirected to a Sinch signup page — that is expected. Your credentials work with Faxbot’s Phaxio flow and the Sinch (direct upload) flow.

Where do I find credentials?
- Sinch dashboard (recommended): https://dashboard.sinch.com
  - Create a project → note “Project ID”
  - Create/locate API key + secret
- Phaxio docs if you prefer the legacy console: https://www.phaxio.com/docs/

Apply & reload
- “Apply” writes settings to the running API with safe defaults.
- “Generate .env” gives you a copy‑ready file. If enabled, “Save .env to server” writes it for persistence.

Helpful tips
- No domain? Pick “Sinch (Direct Upload)” and you can send immediately.
- For Phaxio tests without a domain, run: `scripts/setup-phaxio-tunnel.sh` (uses Cloudflare Tunnel; falls back to ngrok if installed).
- If a change affects persistent connections (e.g., Asterisk AMI), you’ll be prompted to restart the API.
- Secrets are not stored in plugin manifests; they live in environment variables.

Example .env snippets
- Phaxio (HIPAA profile)
```
FAX_BACKEND=phaxio
PHAXIO_API_KEY=... 
PHAXIO_API_SECRET=...
PUBLIC_API_URL=https://yourdomain.example
PHAXIO_CALLBACK_URL=https://yourdomain.example/phaxio-callback
PHAXIO_VERIFY_SIGNATURE=true
API_KEY=generate_a_strong_key
ENFORCE_PUBLIC_HTTPS=true
AUDIT_LOG_ENABLED=true
PDF_TOKEN_TTL_MINUTES=60
```
- Sinch (direct upload)
```
FAX_BACKEND=sinch
SINCH_PROJECT_ID=...
SINCH_API_KEY=...
SINCH_API_SECRET=...
# Optional regional override
# SINCH_BASE_URL=https://us.fax.api.sinch.com/v3
API_KEY=generate_a_strong_key
```
- SIP/Asterisk (self‑hosted)
```
FAX_BACKEND=sip
ASTERISK_AMI_HOST=asterisk
ASTERISK_AMI_PORT=5038
ASTERISK_AMI_USERNAME=api
ASTERISK_AMI_PASSWORD=change_me
FAX_LOCAL_STATION_ID=+15551234567
FAX_HEADER=Faxbot
API_KEY=generate_a_strong_key
```

Warnings and prompts
- Missing HTTPS on `PUBLIC_API_URL` with cloud backends → warning with quick‑fix link to the tunnel script.
- Empty `API_KEY` in production → prompt to enable auth.
- Ghostscript not found for SIP/Asterisk → warn that conversion/pages may be stubbed.

Learn more
- Phaxio: [Backend setup](/Faxbot/backends/phaxio-setup.html) • Official docs: https://www.phaxio.com/docs/
- Sinch: [Backend setup](/Faxbot/backends/sinch-setup.html) • Sign up: https://dashboard.sinch.com/signup • Docs: https://developers.sinch.com/docs/fax/overview/
- SIP/Asterisk: [Backend setup](/Faxbot/backends/sip-setup.html)
- Security: [Authentication](/Faxbot/security/authentication/), [HIPAA](/Faxbot/security/hipaa-requirements.html), [OAuth/OIDC](/Faxbot/security/oauth-setup.html)
