---
layout: default
title: REST API
nav_order: 90
permalink: /api/
nav_exclude: true
---

# REST API (Legacy Page)

This page is superseded by the consolidated API Reference. See:

- {{ site.baseurl }}/development/api-reference.html

Base URL
- Default: `http://localhost:8080`
- Some features require `PUBLIC_API_URL` to be set for cloud backends

Auth
- Header `X-API-Key: <token>` when `API_KEY` is set on the server

POST /fax
- Multipart form: `to` (string), `file` (PDF or TXT)
- Returns 202 Accepted with job info: `{ id, to, status, backend, created_at, updated_at }`
- Errors: 400 invalid `to` or params, 401 auth, 413 too large, 415 unsupported type

GET /fax/{id}
- Returns latest job status and metadata
- Errors: 401 auth, 404 not found

GET /fax/{id}/pdf
- For cloud providers to fetch the PDF
- Query: `token` (per‑job token, optional expiry)
- Errors: 403 invalid/expired token, 404 not available

POST /phaxio-callback
- Provider webhook (form data) with optional signature verification
- Include `?job_id={jobId}` to correlate

Health
- `GET /health` → `{ status: "ok" }`

Notes
- Phone validation accepts E.164 or digits (`+15551234567`, `15551234567`)
- Max upload size is `MAX_FILE_SIZE_MB` (default 10 MB)
- Allowed content types: `application/pdf`, `text/plain`
