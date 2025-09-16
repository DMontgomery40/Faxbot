---
layout: default
title: SIP/Asterisk
parent: Backends
nav_order: 5
permalink: /backends/sip-setup.html
---

# SIP/Asterisk

Choose this backend when you want to keep fax traffic entirely under your control. Faxbot coordinates conversion and job management, while Asterisk handles T.38 calls to your SIP trunk.

## Before you begin

- **SIP trunk with T.38 support** (Twilio Voice, Bandwidth, Telnyx, Flowroute, etc.)
- **Static or well-known public IP** so the provider can reach your host
- Ability to open/forward the following ports to the Asterisk container/host:
  - `5060` TCP/UDP (SIP signalling)
  - `4000–4999` UDP (UDPTL for T.38)
  - `5038` TCP (AMI – keep internal only)
- Docker host with Ghostscript available (Faxbot uses it for PDF→TIFF conversion)

{: .warning }
Never expose AMI (5038) or the Docker API to the internet. Place Faxbot + Asterisk behind a VPN or firewall with explicit allowlists for your SIP provider.

## Configure with the Setup Wizard

1. Admin Console → **Setup Wizard**
2. Choose **SIP/Asterisk**
3. Provide the AMI credentials, SIP trunk auth, caller ID, and optional fax header/station ID
4. Enter your network details (public IP/hostname) so Faxbot can render `pjsip.conf`
5. Apply & reload. The wizard writes `config/faxbot.config.json`, regenerates the Asterisk configs, and restarts both containers.

After applying, open **Diagnostics → Telephony** to confirm registration status and UDPTL port range.

## Network checklist

- Forward/allow UDP `4000–4999` and `5060` to the Asterisk host
- Restrict inbound traffic to the IP ranges your carrier publishes
- If you sit behind CGNAT or cannot forward ports, deploy Faxbot + Asterisk on a cloud VM instead (ensure HIPAA controls if handling PHI)
- Enable TLS signalling or a VPN tunnel when the carrier supports it; sample configurations live under `asterisk/etc/asterisk/templates/`

## Dialplan & AMI overview

- Faxbot originates calls via **Asterisk Manager Interface (AMI)** using the credentials you entered
- The `/asterisk/etc/asterisk/extensions.conf` template sends faxes with `SendFAX()` and emits `UserEvent(FaxResult)` on completion
- AMI events flow back into Faxbot, updating job status inside the Admin Console automatically

## How it works (under the hood)
- API converts TXT→PDF, then PDF→TIFF using Ghostscript
- `originate_sendfax(job_id, to, tiff_path)` is sent over AMI; job status updates to `in_progress`
- On `FaxResult`, the API maps Asterisk fields to `SUCCESS/FAILED` and records page count when present
- Admin coverage: Diagnostics shows AMI connectivity and Ghostscript availability; Jobs detail view surfaces the outbound PDF

## Cutover checklist

Use this when replacing a hosted fax service:

- Confirm T.38 is enabled on the trunk and in the carrier portal
- Assign a static IP or DDNS entry and update firewall/NAT rules
- Keep AMI on an internal network segment; never expose `5038/tcp`
- Populate **Settings → Security** with a strong API key and enable audit logging if PHI is involved
- Run a single-page PDF test to a known good destination before migrating production traffic
- Monitor **Logs → Asterisk** for `FaxResult` events during the pilot

## Troubleshooting

- **Registration fails** → Check SIP credentials and NAT settings; Diagnostics shows the last registration attempt
- **Fax stalls mid-call** → Verify the UDPTL port range is open end-to-end and that Ghostscript produced the TIFF (Logs → API)
- **Jobs stay queued** → AMI credentials are wrong or port 5038 is blocked; rerun the wizard and confirm AMI events appear in Diagnostics
- **Need CLI access?** → `docker compose logs -f asterisk` exposes live dialplan traces. Use sparingly and redact numbers in shared logs.

## References

- Asterisk Fax overview: https://wiki.asterisk.org/wiki/display/AST/Fax
- SendFAX application: https://wiki.asterisk.org/wiki/display/AST/Application_SendFAX
- AMI documentation: https://wiki.asterisk.org/wiki/display/AST/Asterisk+Manager+Interface+(AMI)

Ready to self-host with FreeSWITCH instead? Continue to the [FreeSWITCH backend](freeswitch-setup.html).
