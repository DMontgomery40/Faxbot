# SINCH_SETUP.md

Cloud backend using Sinch Fax API v3 ("Phaxio by Sinch"). This backend uploads your PDF directly to Sinch rather than serving a tokenized URL.

When to use
- Prefer this if you have a Sinch account/project and want the v3 direct‑upload flow.
- If you signed up at Phaxio and were redirected to Sinch, your credentials generally work here. You will also need your Sinch Project ID.

Key differences vs `phaxio` backend
- `phaxio`: Provider fetches your PDF via `PUBLIC_API_URL` and posts status to `/phaxio-callback` (HMAC verification supported). No Sinch project ID required.
- `sinch`: Faxbot uploads your PDF directly to Sinch (multipart). PUBLIC_API_URL and `/phaxio-callback` are not used. Webhook integration for Sinch is under evaluation; current builds reflect the provider’s initial status response.

Environment
```
FAX_BACKEND=sinch
SINCH_PROJECT_ID=your_project_id
SINCH_API_KEY=your_api_key          # falls back to PHAXIO_API_KEY if unset
SINCH_API_SECRET=your_api_secret    # falls back to PHAXIO_API_SECRET if unset
# Optional override region/base URL:
# SINCH_BASE_URL=https://fax.api.sinch.com/v3

# General
API_KEY=your_secure_api_key         # optional but recommended (X-API-Key)
MAX_FILE_SIZE_MB=10
```

Send a fax (curl)
```
curl -X POST http://localhost:8080/fax \
  -H "X-API-Key: $API_KEY" \
  -F to=+15551234567 \
  -F file=@./example.pdf
```
The response includes a job ID and the `backend: "sinch"` field.

Status updates
- Immediate status is mapped from Sinch’s response. Additional webhook handling may be added later; for now, poll your own app state via `GET /fax/{id}`.

Notes
- Only PDF and TXT files are accepted. Convert images (PNG/JPG) to PDF first.
- Avoid exposing credentials. Place Faxbot behind HTTPS and a reverse proxy with rate limiting.

Troubleshooting
- 401: invalid API key to your Faxbot API (set `API_KEY` and send `X-API-Key`).
- 413: file too large → raise `MAX_FILE_SIZE_MB`.
- 415: unsupported file type → only PDF/TXT.
- Sinch API errors: verify Project ID, API key/secret, and region.
---
layout: default
title: SINCH_SETUP.md
nav_order: 70
---
