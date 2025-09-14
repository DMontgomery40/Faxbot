---
layout: default
title: Phaxio Go‑Live
parent: Go‑Live Checklists
nav_order: 1
permalink: /go-live/phaxio/
---

# Phaxio Go‑Live Checklist

Accounts & Legal
- Phaxio account active; credentials verified
- BAA executed (for HIPAA)
- Provider storage disabled in Phaxio console (HIPAA)

Faxbot Configuration
- Backend set to `phaxio`
- `PHAXIO_API_KEY` and `PHAXIO_API_SECRET` set
- `PUBLIC_API_URL` points to HTTPS domain
- Callback URL set to `<PUBLIC_API_URL>/phaxio-callback` and configured in Phaxio console
- Signature verification enabled: `PHAXIO_VERIFY_SIGNATURE=true` (HIPAA)

Security
- `API_KEY` set and clients include `X-API-Key`
- `ENFORCE_PUBLIC_HTTPS=true` (HIPAA)
- Audit logging enabled if required by policy
- `PDF_TOKEN_TTL_MINUTES` set appropriately (default 60)

Networking & Reachability
- Public DNS/TLS validated (no self‑signed certificates in production)
- Callback and PDF token URL accessible to Phaxio

Smoke Tests
- Send test fax from Admin Console and via SDK (Node/Python)
- Observe status update via `/phaxio-callback`
- Confirm pages reported and final status SUCCESS/FAILED as expected

Runbooks
- On callback failures: check signature header, endpoint reachability, and secrets
- On PDF fetch failures: check token, TTL, and public HTTPS URL

