---
layout: default
title: Phaxio Setup
parent: Backends
nav_order: 2
permalink: /backends/phaxio-setup.html
---

# Phaxio Setup

Phaxio (part of Sinch) is the recommended cloud backend for healthcare teams that need HIPAA guarantees with minimal effort. Everything is configured from the Admin Console—no manual `.env` edits once the container is running.

## Gather before you start

- **API Key** and **API Secret** from the Phaxio/Sinch dashboard
- A public **HTTPS** URL so Phaxio can fetch PDFs and post callbacks (tunnel or domain)
- Optional: BAA confirmation from `compliance@phaxio.com`

## Configure through the Setup Wizard

1. Admin Console → **Setup Wizard**
2. Choose **Phaxio (Recommended)**
3. Fill credentials and pick your security posture
   - *HIPAA profile* locks HTTPS on, enables HMAC verification, and disables provider storage hints
   - *Non-PHI profile* keeps verification optional for quick testing
4. Provide the public URL that Phaxio should call. The wizard derives the callback endpoint automatically.
5. Review the summary and click **Apply & Reload**. Faxbot writes `config/faxbot.config.json`, restarts the API, and confirms health.

{: .note }
Evaluating locally? Use a tunnel such as Cloudflare or ngrok—the wizard links directly to [Public Access & Tunnels]({{ site.baseurl }}/backends/public-access.html) with a one-command helper.

## First fax in the Admin Console

1. Navigate to **Send Fax**
2. Upload a PDF or TXT (10 MB max). Faxbot will convert TXT→PDF for you.
3. Enter an E.164 phone number (`+15551234567`) and submit.
4. Watch **Jobs** for real-time status. When Phaxio hits the callback, the row flips from `in_progress` to `SUCCESS` or `FAILED` with a remediation link.

The Node and Python SDKs mirror this workflow once you are satisfied with the console experience.

## How it works (under the hood)

- Faxbot hosts a tokenised PDF URL; Phaxio fetches it using your `PUBLIC_API_URL`
- Status changes arrive at `/phaxio-callback` with HMAC-SHA256 signatures
- Tokens expire based on the retention policy you pick in **Settings → Storage**
- Signature header: `X-Phaxio-Signature` (HMAC-SHA256 over raw body with `PHAXIO_API_SECRET`)
- Admin coverage: Diagnostics shows callback URL and signature status; the wizard sets `PHAXIO_CALLBACK_URL` and `PHAXIO_VERIFY_SIGNATURE`

## Troubleshooting

- **No status updates** → Verify the public URL is reachable and that signature verification remains enabled; the wizard shows the exact callback value on the confirmation screen.
- **403 fetching PDF** → The token expired. Reopen the job in the Admin Console to mint a fresh link.
- **Credential errors** → Run the Setup Wizard again. Faxbot validates the key/secret pair before saving.

## References

- Signup (redirects to Sinch): https://console.phaxio.com/signup
- Dashboard: https://dashboard.sinch.com
- HIPAA details: https://www.phaxio.com/docs/security/hipaa
- Classic API overview: https://www.phaxio.com/docs/api/

Prefer the direct-upload Sinch Fax API v3? Use the [Sinch backend](sinch-setup.html) instead.
