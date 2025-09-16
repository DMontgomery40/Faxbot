---
layout: default
title: Sinch Fax API v3
parent: Backends
nav_order: 1
permalink: /backends/sinch-setup.html
---

# Sinch Fax API v3

Use this backend when you want Sinch's direct-upload workflow (often branded "Phaxio by Sinch"). Faxbot streams your PDF to Sinch immediately, so you do **not** need a public URL for the provider to fetch from.

## Gather before you start

- Sinch Project ID
- API Key and Secret (same values Phaxio dashboards expose)
- Optional: `SINCH_BASE_URL` if you are on a non-default region

## Configure through the Setup Wizard

1. Admin Console → **Setup Wizard**
2. Choose **Sinch Fax API v3**
3. Enter Project ID, API Key, and API Secret
4. Pick HIPAA vs Non-PHI profile. HIPAA profile enforces HTTPS when you later enable inbound receiving or webhooks.
5. Apply the changes. Faxbot stores credentials in the plugin config and validates them immediately.

## Send & monitor entirely from the console

1. Go to **Send Fax** and upload a PDF or TXT (10 MB limit)
2. Enter the destination number (`+15551234567` format)
3. Submit. Sinch returns a job reference instantly, which Faxbot maps to your internal job ID.
4. Watch **Jobs**. Status moves from `queued` → `in_progress` → `SUCCESS/FAILED` based on Sinch responses.

## Key differences vs the Phaxio backend

- PDFs upload via Sinch's REST API; no tokenised URL is exposed
- Callbacks are optional. When enabled, they land on `/sinch-callback` with signature verification (configure the signing secret under **Settings → Backends → Sinch**)
- Perfect for temporary lab environments where you cannot expose a public HTTPS endpoint

## Troubleshooting

- **Credentials rejected** → Double-check the Project ID; Sinch requires the UUID-style ID, not the workspace name
- **413 file too large** → Increase `MAX_FILE_SIZE_MB` in Settings or compress the PDF
- **Pending forever** → Ensure the Sinch console shows the job; if not, the API likely rejected the request (Admin Console Diagnostics → Provider Logs).

## References

- Sign up / dashboard: https://dashboard.sinch.com
- Fax API overview: https://developers.sinch.com/docs/fax/overview/
- Regional endpoints: https://developers.sinch.com/docs/fax/api-reference/#base-urls

Switching back to tokenised fetch flows? Use the [Phaxio backend](phaxio-setup.html).
