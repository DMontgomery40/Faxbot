---
layout: default
title: Phaxio — One‑Pager (Print)
parent: Go‑Live Checklists
nav_order: 99
permalink: /go-live/phaxio/one-pager/
---

<style>
@media print {
  .side-bar, .site-nav, .site-header, .page-header, .page-footer, .search, nav { display: none !important; }
  .main-content { margin: 0 !important; padding: 0 !important; }
}
.sig-line { border-bottom: 1px solid #999; display: inline-block; min-width: 280px; }
</style>

# Phaxio Go‑Live — One‑Pager

Organization: _________  Environment: Dev / Staging / Prod  Date: _________

Contacts
- Owner: ___________  On‑call: ___________  Provider Account: ___________

Accounts & Legal
- [ ] Phaxio account active (console access verified)
- [ ] BAA executed (if handling PHI)
- [ ] Provider document storage disabled (HIPAA)

Faxbot Configuration
- [ ] `FAX_BACKEND=phaxio`
- [ ] `PHAXIO_API_KEY` and `PHAXIO_API_SECRET` set
- [ ] `PUBLIC_API_URL` uses HTTPS (resolves publicly)
- [ ] Callback URL set to `<PUBLIC_API_URL>/phaxio-callback`
- [ ] Signature verification enabled: `PHAXIO_VERIFY_SIGNATURE=true` (HIPAA)
- [ ] `API_KEY` set; clients send `X-API-Key`

Security
- [ ] `ENFORCE_PUBLIC_HTTPS=true` (HIPAA)
- [ ] `PDF_TOKEN_TTL_MINUTES` set appropriately (default 60)
- [ ] Audit logging enabled per policy (if required)

Networking
- [ ] DNS and TLS valid (no warnings)
- [ ] Callback reachable from internet

Smoke Test
- [ ] Admin Console → Send: test PDF to known number
- [ ] Status updates received via `/phaxio-callback`
- [ ] Pages count matches expectations

Runbooks
- Callback failure → verify signature header, endpoint reachability, secrets
- PDF fetch failure → check token/TTL and `PUBLIC_API_URL`

Approvals
- Security sign‑off: <span class="sig-line"></span>
- Operations sign‑off: <span class="sig-line"></span>

References: Phaxio API v2.1 (create/send, webhooks), HIPAA — see Third‑Party page.

