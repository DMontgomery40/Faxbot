---
layout: default
title: FreeSWITCH Fax (Self-Hosted)
parent: Backends
nav_order: 6
permalink: /backends/freeswitch-setup.html
---

# FreeSWITCH Fax (Self-Hosted)

Use this backend when your telephony stack is already built on FreeSWITCH and you want Faxbot to orchestrate fax jobs via `txfax`.

## Prerequisites

- FreeSWITCH with `mod_spandsp` enabled
- Working SIP gateway (`sofia/gateway/<name>`) that supports T.38
- Network path from Faxbot → FreeSWITCH (often inside the same LAN or docker network)
- Admin access to your dialplan to add the completion hook

## Configure Faxbot

1. Admin Console → **Setup Wizard**
2. Choose **FreeSWITCH**
3. Fill the gateway name, caller ID, and any authentication required
4. Apply & reload. Faxbot stores the config and shows the expected hook in the confirmation step.

## Dialplan hook

Add the following action to your outbound fax dialplan to post results back to Faxbot:

```xml
<action application="set" data="api_hangup_hook=system curl -s -X POST \
  -H 'Content-Type: application/json' \
  -H 'X-Internal-Secret: YOUR_SECRET' \
  -d '{"job_id":"${faxbot_job_id}","fax_status":"${fax_success}","fax_result_text":"${fax_result_text}","fax_document_transferred_pages":${fax_document_transferred_pages},"uuid":"${uuid}"}' \
  http://api:8080/_internal/freeswitch/outbound_result"/>
```

- Replace `YOUR_SECRET` with the value shown in the Setup Wizard (maps to `ASTERISK_INBOUND_SECRET`)
- When running in Docker Compose the Faxbot API service is reachable as `http://api:8080`; otherwise point to your actual host

Faxbot queues jobs, generates TIFF artifacts, and triggers `bgapi originate ... &txfax(...)`. The hook above confirms success/failure so the Admin Console can update status instantly.

## Security notes

- Keep `fs_cli`/ESL access restricted to a private network
- Use TLS or a VPN to reach your SIP gateway whenever the carrier supports it
- Disable any FreeSWITCH document storage to avoid retaining PHI longer than necessary

## Troubleshooting

- **Hook never fires** → Ensure `api_hangup_hook` has no quoting issues (copy from the wizard). Logs → FreeSWITCH show the command execution.
- **Jobs stuck in progress** → Faxbot never received the webhook; verify the secret header and URL.
- **TIFF missing** → Check Faxbot API logs for Ghostscript conversion output.

More FreeSWITCH context lives in [Faxbot third-party references]({{ site.baseurl }}/third-party/).
