# PHAXIO_SETUP.md

## Overview
- Cloud backend for sending faxes via Phaxio (also branded “Phaxio by Sinch”).
- Easiest option; no SIP or telephony expertise required.
- Send-only (no receiving).

## Prerequisites
- Phaxio account and API credentials.
- Public URL for callbacks and PDF access (domain or tunnel like ngrok).
- Docker and Docker Compose installed.

## Steps
1) Create Phaxio account and get credentials
- Log in to the Phaxio console and retrieve:
  - PHAXIO_API_KEY
  - PHAXIO_API_SECRET

Note on branding: New Phaxio signups and dashboards may redirect to Sinch. That is expected — Phaxio is a Sinch company. This backend continues to work with those credentials.

2) Set environment variables
- Edit `.env` (or create from `.env.example`). Set:
```
FAX_BACKEND=phaxio
PHAXIO_API_KEY=your_key
PHAXIO_API_SECRET=your_secret
PUBLIC_API_URL=https://your-domain.com
PHAXIO_CALLBACK_URL=https://your-domain.com/phaxio-callback   # Preferred name
# PHAXIO_STATUS_CALLBACK_URL=https://your-domain.com/phaxio-callback  # Alias also supported
API_KEY=your_secure_api_key   # Optional but recommended; used as X-API-Key
```
- Note: PUBLIC_API_URL must be reachable by Phaxio to fetch PDFs.
 - For production, set `ENFORCE_PUBLIC_HTTPS=true` to require HTTPS (recommended). For local testing, leave it false.

Sinch v3 vs legacy Phaxio: If you prefer Sinch’s Fax API v3 “direct upload” flow, use the separate `sinch` backend instead (see SINCH_SETUP.md). This guide covers the classic Phaxio-style flow where the provider fetches your PDF via a tokenized URL and posts status to `/phaxio-callback`.

3) Start the API
```
make up-cloud   # or: docker compose up -d --build api
```
- API will listen on `http://localhost:8080` by default.

How this works: you talk to the Faxbot API (your local/server endpoint). Faxbot then calls the official Phaxio API on your behalf and gives Phaxio a public URL to fetch your PDF. You do not call Phaxio endpoints directly from your client. Ensure `PUBLIC_API_URL` is reachable from Phaxio and that your callback URL (`PHAXIO_CALLBACK_URL` or `PHAXIO_STATUS_CALLBACK_URL`) points back to your server.

4) Test sending a fax
- Convert TXT→PDF→TIFF is handled automatically.
- Example (replace number):
```
curl -X POST http://localhost:8080/fax \
  -H "X-API-Key: your_secure_api_key" \
  -F to=+15551234567 \
  -F file=@./example.pdf
```
- Response includes `id`, `status`, `backend`, timestamps.
- Check status:
```
curl -H "X-API-Key: your_secure_api_key" http://localhost:8080/fax/<job_id>
```

5) Configure callback (optional but recommended)
- Phaxio will POST status to your callback URL (`PHAXIO_CALLBACK_URL` or `PHAXIO_STATUS_CALLBACK_URL`).
- This API exposes `POST /phaxio-callback` and will update job status when the request includes `?job_id=<id>`.
- Ensure your PUBLIC_API_URL and callback URL are reachable from Phaxio.
- Security: by default, callbacks must include a valid `X-Phaxio-Signature` (HMAC-SHA256 of the raw body using `PHAXIO_API_SECRET`). You can disable this by setting `PHAXIO_VERIFY_SIGNATURE=false` (not recommended).
 - Optional retention: set `ARTIFACT_TTL_DAYS>0` to automatically delete PDFs after the specified number of days (cleanup runs daily by default).

## Costs & HIPAA
- Phaxio pricing: see their site for per-page costs.
- HIPAA information (BAA): https://www.phaxio.com/docs/security/hipaa

## Security Notes
- Set a strong `API_KEY` and send it as `X-API-Key`.
- Rate limit and restrict access via reverse proxy (nginx, Caddy, etc.).
- The PDF serving endpoint uses a tokenized URL; treat PUBLIC_API_URL as sensitive.
- Use HTTPS for `PUBLIC_API_URL` in production so Phaxio fetches over TLS. HTTP is fine for local development only.

## Number Format
- Use E.164 format (e.g., `+15551234567`) for best results.
- The backend performs limited normalization for non‑E.164 input, but E.164 avoids ambiguity across regions.

## Troubleshooting
- "phaxio not configured": verify `FAX_BACKEND=phaxio` and both API key/secret.
- No status updates: confirm callback URL and public reachability.
- 403 when fetching PDF: token mismatch or expired URL.
- See docs/TROUBLESHOOTING.md for more.

## Related: Sinch Fax API v3
Phaxio is part of Sinch. If your console shows Sinch and you prefer the v3 API’s direct upload model (and features like their own webhooks), use the `sinch` backend. See SINCH_SETUP.md. Your existing Phaxio credentials typically work as Sinch API credentials; you will also need the Sinch Project ID.
---
layout: default
title: PHAXIO_SETUP.md
nav_order: 60
---
