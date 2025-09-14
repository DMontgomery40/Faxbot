---
layout: default
title: Sinch — One‑Pager (Print)
parent: Go‑Live Checklists
nav_order: 99
permalink: /go-live/sinch/one-pager/
---

<style>
@media print {
  .side-bar, .site-nav, .site-header, .page-header, .page-footer, .search, nav { display: none !important; }
  .main-content { margin: 0 !important; padding: 0 !important; }
}
.sig-line { border-bottom: 1px solid #999; display: inline-block; min-width: 280px; }
</style>

# Sinch Go‑Live — One‑Pager

Organization: _________  Environment: Dev / Staging / Prod  Date: _________

Accounts & Project
- [ ] Sinch project with Fax API enabled
- [ ] `SINCH_PROJECT_ID` documented; API key/secret verified
- [ ] `SINCH_BASE_URL` set if using regional endpoint

Faxbot Configuration
- [ ] `FAX_BACKEND=sinch`
- [ ] `API_KEY` set; clients send `X-API-Key`

Security
- [ ] TLS termination in front of API (HTTPS endpoints)
- [ ] Audit logging enabled if policy requires

Smoke Test
- [ ] Admin Console → Send: test PDF
- [ ] SDK send (Node/Python) returns expected status

Runbooks
- Create fax error → verify project/region and credentials
- Status handling → understand initial provider response vs eventual delivery

Approvals
- Security sign‑off: <span class="sig-line"></span>
- Operations sign‑off: <span class="sig-line"></span>

References: Sinch Fax overview & API reference — see Third‑Party page.

