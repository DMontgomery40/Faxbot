---
layout: default
title: SIP/Asterisk Go‑Live
parent: Go‑Live Checklists
nav_order: 3
permalink: /go-live/sip-asterisk/
---

# SIP/Asterisk Go‑Live Checklist

Telephony & Network
- SIP trunk provider supports T.38
- Static IP/DDNS configured; firewall allows provider IPs to `5060` (SIP) and `4000‑4999` (UDPTL)
- AMI (5038) is private/internal only

Asterisk
- Dialplan matches `asterisk/etc/asterisk/extensions.conf` (faxout/faxsend contexts)
- `UserEvent(FaxResult, ...)` emitted on completion

Faxbot
- Backend set to `sip`
- AMI settings verified: host/port/user/password
- Ghostscript installed on API host for PDF→TIFF

Security
- `API_KEY` enabled; reverse proxy applies rate limits
- Audit logging as required by policy

Smoke Tests
- Single‑page PDF to a known good number
- Observe AMI events and final status in Jobs

Runbooks
- On AMI login failure: verify credentials and network path
- On TIFF errors: confirm Ghostscript binary and permissions

