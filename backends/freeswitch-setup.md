---
layout: default
title: FreeSWITCH Fax (Self‑Hosted)
parent: Backends
nav_order: 5
permalink: /backends/freeswitch-setup.html
---

# FreeSWITCH Fax (Self‑Hosted)

Who is this for
- Self‑hosters who want full control using FreeSWITCH and a SIP trunk (T.38 preferred).

What you’ll do
- Point Faxbot at FreeSWITCH for outbound faxing via `txfax`, and configure a dialplan hook to post results back to Faxbot.

Prerequisites
- FreeSWITCH with `mod_spandsp` and an outbound SIP gateway (`sofia/gateway/<name>`)
- fs_cli on the Faxbot host (or an ESL listener in future)

Steps
1) FreeSWITCH basics
   - Ensure your gateway registers (e.g., `gw_signalwire`) and T.38 is enabled where possible
   - Typical outbound fragment uses `&txfax(/path/to/file.tiff)`
2) Configure Faxbot
   - Admin Console → Settings → Backend: `freeswitch`
   - Set gateway, caller ID, and T.38 toggle as needed
   - Click “Apply & Reload”
3) Result hook (copyable)
   - In your outbound dialplan, add this to post results back to Faxbot:

```
<action application="set" data="api_hangup_hook=system curl -s -X POST \
  -H 'Content-Type: application/json' \
  -H 'X-Internal-Secret: YOUR_SECRET' \
  -d '{\"job_id\":\"${faxbot_job_id}\",\"fax_status\":\"${fax_success}\",\"fax_result_text\":\"${fax_result_text}\",\"fax_document_transferred_pages\":${fax_document_transferred_pages},\"uuid\":\"${uuid}\"}' \
  http://api:8080/_internal/freeswitch/outbound_result"/>
```

   - Replace `YOUR_SECRET` with `ASTERISK_INBOUND_SECRET` in your `.env`
   - Use `api` as the host when Faxbot runs in Docker Compose; otherwise set your API host

4) Send a test fax
   - Admin Console → Send (PDF/TXT)
   - Faxbot converts PDF→TIFF and dispatches `bgapi originate … &txfax`
   - On call completion, the hook updates job status in Faxbot

Security & HIPAA
- Keep FS and `fs_cli` in private networks; do not expose ESL
- Use T.38 where possible; prefer TLS on signaling with your provider

References
- FreeSWITCH fax and dialplan: see [Third‑Party References](/Faxbot/third-party/)

