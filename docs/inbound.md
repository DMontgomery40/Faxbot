---
layout: default
title: Inbound Receiving
nav_order: 40
permalink: /inbound/
---

# Inbound Receiving

Receive faxes from cloud providers or a self‑hosted Asterisk stack, store PDFs, and access them securely with short‑lived tokens.

Enable
- Set `INBOUND_ENABLED=true` in Settings or the Setup Wizard
- Choose storage: `STORAGE_BACKEND=local|s3` (S3 supports SSE‑KMS and S3‑compatible endpoints)
- Configure token TTL: `INBOUND_TOKEN_TTL_MINUTES` (default 60)
- Configure retention: `INBOUND_RETENTION_DAYS` (default 30)

Provider callbacks
- Phaxio: `POST /phaxio-inbound` with HMAC verification (enabled by default via `PHAXIO_INBOUND_VERIFY_SIGNATURE=true`)
- Sinch: `POST /sinch-inbound` with optional Basic (`SINCH_INBOUND_BASIC_USER/PASS`) and/or HMAC (`SINCH_INBOUND_HMAC_SECRET`)
- SIP/Asterisk (internal): `POST /_internal/asterisk/inbound` with `X-Internal-Secret: <ASTERISK_INBOUND_SECRET>` and JSON `{ tiff_path, to_number, from_number?, faxstatus?, faxpages?, uniqueid }`

Access (scoped)
- `GET /inbound` — list inbound faxes (scope `inbound:list`, per‑key RPM limit)
- `GET /inbound/{id}` — metadata (scope `inbound:read`)
- `GET /inbound/{id}/pdf` — tokenized PDF access via `?token=...` or with `X-API-Key` + `inbound:read`

Storage
- Local: stores PDFs under `FAX_DATA_DIR` (dev only)
- S3/S3‑compatible: provide `S3_BUCKET`, `S3_REGION`, optional `S3_PREFIX`, `S3_ENDPOINT_URL`, `S3_KMS_KEY_ID`
- For HIPAA, use SSE‑KMS and lifecycle rules; keep buckets private

Admin Console
- Toggle inbound, configure storage (local vs S3), token TTL, and retention
- Inbound list/detail views with secure download links
- Diagnostics show callback URLs, signature expectations, and rate limits

Notes
- Keep PHI secure end‑to‑end: TLS for callbacks, HMAC verification, strict auth
- Token TTL defaults to 60 minutes; reduce where feasible
