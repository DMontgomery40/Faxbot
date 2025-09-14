---
layout: default
title: SIP/Asterisk — One‑Pager (Print)
parent: Go‑Live Checklists
nav_order: 99
permalink: /go-live/sip-asterisk/one-pager/
---

<style>
@media print {
  .side-bar, .site-nav, .site-header, .page-header, .page-footer, .search, nav { display: none !important; }
  .main-content { margin: 0 !important; padding: 0 !important; }
}
.sig-line { border-bottom: 1px solid #999; display: inline-block; min-width: 280px; }
</style>

# SIP/Asterisk Go‑Live — One‑Pager

Organization: _________  Environment: Dev / Staging / Prod  Date: _________

Telephony & Network
- [ ] SIP trunk supports T.38 (UDPTL)
- [ ] Static IP/DDNS configured
- [ ] Ports open: 5060 (tcp+udp), 4000–4999/udp (UDPTL) — provider IPs only
- [ ] AMI (5038) private/internal only

Asterisk
- [ ] Dialplan configured (faxout/faxsend, SendFAX)
- [ ] `UserEvent(FaxResult, ...)` emitted

Faxbot
- [ ] `FAX_BACKEND=sip`
- [ ] AMI settings verified (host/port/user/password)
- [ ] Ghostscript installed (PDF→TIFF)
- [ ] `API_KEY` set; clients send `X-API-Key`

Smoke Test
- [ ] Single‑page PDF to known good number
- [ ] AMI events observed; final status updated in Jobs

Runbooks
- AMI login failure → check credentials, firewall
- TIFF conversion errors → confirm Ghostscript

Approvals
- Security sign‑off: <span class="sig-line"></span>
- Operations sign‑off: <span class="sig-line"></span>

References: Asterisk AMI, Fax, SendFAX — see Third‑Party page.

