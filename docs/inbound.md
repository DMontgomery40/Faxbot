---
layout: default
title: Inbound (WIP)
nav_order: 40
permalink: /inbound/
---

# Inbound (Work in Progress)

Inbound receiving scaffolding is planned per v3 design. Current API focuses on outbound.

Planned shape
- Enable via `INBOUND_ENABLED=true`
- Cloud callbacks:
  - Phaxio: `POST /phaxio-inbound` with HMAC verification
  - Sinch: `POST /sinch-inbound` (Basic and/or HMAC)
- Self‑hosted (SIP/Asterisk): `POST /_internal/asterisk/inbound` on private network
- Storage backends: `STORAGE_BACKEND=local|s3` with KMS and S3‑compatible endpoints
- Access: `GET /inbound`, `GET /inbound/{id}`, `GET /inbound/{id}/pdf` (tokenized)

Notes
- Keep PHI secure; use S3 with SSE‑KMS in production
- UI: enablement toggles, retention, token TTLs, and scoped access

