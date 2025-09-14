---
layout: default
title: Sinch Go‑Live
parent: Go‑Live Checklists
nav_order: 2
permalink: /go-live/sinch/
---

# Sinch Go‑Live Checklist

Accounts & Project
- Sinch project with Fax API enabled
- Credentials verified: `SINCH_PROJECT_ID`, `SINCH_API_KEY`, `SINCH_API_SECRET`
- Region selected; base URL correct (`SINCH_BASE_URL` if overriding)

Faxbot Configuration
- Backend set to `sinch`
- API key configured for clients

Security
- TLS termination in front of API
- Audit logging enabled if required by policy

Smoke Tests
- Admin Console send → status reflects provider response
- SDK send (Node/Python)

Runbooks
- HTTP errors on create fax: check project/region and credentials
- Status polling: plan for provider state transitions; webhook evaluation pending (track releases)

