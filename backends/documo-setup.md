---
layout: default
title: Documo (mFax)
parent: Backends
nav_order: 3
permalink: /backends/documo-setup.html
---

# Documo (mFax)

Documo’s mFax API is another fast-start cloud option. Like Sinch, Faxbot uploads your files directly so you can stay entirely private behind a tunnel or LAN.

## Gather before you start

- Documo API Key (Dashboard → Developers)
- Decide whether you are pointing at **Production** or **Sandbox**
- Optional base URL override if Documo provisions a regional endpoint

## Configure with the Setup Wizard

1. Admin Console → **Setup Wizard**
2. Choose **Documo (mFax)**
3. Enter your API Key and toggle Sandbox mode if needed
4. Apply. Faxbot validates the key with a lightweight call before saving

## Send a fax from the UI

1. Open **Send Fax**
2. Upload PDF/TXT → enter destination number
3. Submit. Faxbot pushes the file to Documo immediately and shows the Documo job ID in Diagnostics
4. Use **Jobs** to monitor completion; the grid links to Documo’s status page when available

## Security & compliance

- Faxbot never stores Documo credentials in logs; they live in the plugin config store
- Storage defaults to “no provider retention.” Double-check in Documo that document storage is disabled when handling PHI
- Rate-limit inbound API traffic via your reverse proxy like any production deployment

## Troubleshooting

- **Job never appears** → Confirm you chose the correct environment (Production vs Sandbox)
- **401 from provider** → Regenerate the API key and rerun the wizard
- **413 file too large** → Adjust the Faxbot `MAX_FILE_SIZE_MB` setting under **Settings → File Handling**

## References

- Sign up & pricing: <https://www.mfax.io/pricing>
- API documentation: <https://docs.documo.com>

## How it works (under the hood)
- Faxbot uploads the PDF directly via Documo’s REST API using your API key
- Status is derived from the provider’s response and optional follow-up checks
- Admin coverage: Diagnostics shows the configured environment and basic auth presence; Jobs store the provider job ID
