---
layout: default
title: SignalWire Compatibility API
parent: Backends
nav_order: 4
permalink: /backends/signalwire-setup.html
---

# SignalWire Compatibility API

SignalWire’s “Compatibility” endpoints mimic the old Twilio Fax API. Faxbot uses a tokenised MediaUrl so SignalWire can fetch documents securely.

## Gather before you start

- Space URL (e.g., `example.signalwire.com`)
- Project ID and API Token
- Public HTTPS URL for callbacks and PDF fetches

## Configure with the Setup Wizard

1. Admin Console → **Setup Wizard**
2. Choose **SignalWire Compatibility**
3. Enter Space URL, Project ID, API Token, and default From number
4. Provide your public URL; the wizard computes the MediaUrl that SignalWire fetches
5. Apply & reload the config

{: .note }
Need a quick tunnel? Follow [Public Access & Tunnels]({{ site.baseurl }}/backends/public-access.html).

## Send & monitor

- **Send Fax** uploads PDF/TXT, then Faxbot hosts the document at `/fax/{jobId}/pdf?token=…`
- SignalWire fetches the MediaUrl via HTTPS
- Status callbacks hit `/signalwire-callback`; enable signature verification by setting the signing key in **Settings → Backends → SignalWire**
- Watch status transitions in **Jobs** with provider-specific troubleshooting links

## How it works (under the hood)
- Faxbot mints a short‑TTL tokenised MediaUrl and includes it in the create‑fax request
- SignalWire fetches the MediaUrl via HTTPS; Faxbot returns the PDF if and only if token and TTL match
- Optional HMAC verification protects callbacks (`X-SignalWire-Signature` when configured)
- Admin coverage: Diagnostics shows callback URL and signature settings; Jobs reveal provider SID

## Troubleshooting

- **403 fetching MediaUrl** → Token expired or `PUBLIC_API_URL` mismatch; reopen the job to mint a new link
- **401 from SignalWire** → Regenerate the API token in the SignalWire console and rerun the wizard
- **No callbacks** → Confirm the StatusCallback URL is set in the SignalWire portal (the wizard shows the expected value)

## References

- SignalWire Fax docs: https://developer.signalwire.com/
- Compatibility API overview: https://developer.signalwire.com/apis/docs/fax
