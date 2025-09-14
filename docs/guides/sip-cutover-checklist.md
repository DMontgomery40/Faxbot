---
layout: default
title: SIP/Asterisk Cutover Checklist
parent: Guides
nav_order: 2
permalink: /guides/sip-cutover-checklist/
---

# SIP/Asterisk Cutover Checklist

Audience
- Telephony admins preparing to move from a hosted fax service to self‑hosted.

Pre‑flight
- Confirm SIP trunk supports T.38
- Assign static IP or DDNS; configure firewall
- Open ports: `5060` (SIP), `4000‑4999` (UDPTL) to trunk provider IPs only
- Isolate AMI (5038) — private network only

Server
- Install Ghostscript (`gs`) on Faxbot API host
- Configure Asterisk dialplan per `asterisk/etc/asterisk/extensions.conf`
- Verify AMI credentials: `ASTERISK_AMI_HOST/PORT/USERNAME/PASSWORD`

Faxbot Settings
- Backend: SIP/Asterisk
- Presentation: `FAX_LOCAL_STATION_ID`, `FAX_HEADER`
- Security: set `API_KEY`; consider audit logging

Tests
- Single‑page PDF send to a known good destination
- Observe AMI events: `UserEvent(FaxResult...)`
- Validate pages/status on `GET /fax/{id}`

Rollback Plan
- Keep prior hosted service active until acceptance tests pass

References
- [Backend setup](/Faxbot/backends/sip-setup.html)
- [Deployment](/Faxbot/deployment/)

