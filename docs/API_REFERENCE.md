# API_REFERENCE.md

## Base URL
- Default: `http://localhost:8080`
- Health: `GET /health` → `{ "status": "ok" }`

## Auth
- Header `X-API-Key: <key>` if `API_KEY` is set in environment.
- If `API_KEY` is blank, auth is disabled (not recommended).

## Endpoints

1) POST `/fax`
- Multipart form
  - `to`: destination number (E.164 or digits)
  - `file`: PDF or TXT
- Responses
  - 202 Accepted: `{ id, to, status, error?, pages?, backend, provider_sid?, created_at, updated_at }`
  - 400 bad number; 413 file too large; 415 unsupported type; 401 invalid API key
- Example
```
curl -X POST http://localhost:8080/fax \
  -H "X-API-Key: $API_KEY" \
  -F to=+15551234567 \
  -F file=@./example.pdf
```

2) GET `/fax/{id}`
- Returns job status as above.
- 404 if not found; 401 if invalid API key.
```
curl -H "X-API-Key: $API_KEY" http://localhost:8080/fax/$JOB_ID
```

3) GET `/fax/{id}/pdf?token=...`
- Serves the original PDF for cloud provider to fetch.
- No API auth; requires token that matches stored URL.
- 403 invalid/expired token; 404 not found.

4) POST `/phaxio-callback`
- For Phaxio status webhooks. Expects form-encoded fields (e.g., `fax[status]`, `fax[id]`).
- Correlation via query param `?job_id=...`.
- Returns `{ status: "ok" }`.
- Signature verification: if `PHAXIO_VERIFY_SIGNATURE=true` (default), the server verifies `X-Phaxio-Signature` (HMAC-SHA256 of the raw body using `PHAXIO_API_SECRET`). Requests without a valid signature are rejected (401).

## Models
- FaxJobOut
  - `id: string`
  - `to: string`
  - `status: string` (queued | in_progress | SUCCESS | FAILED | disabled)
  - `error?: string`
  - `pages?: number`
  - `backend: string` ("phaxio" or "sip")
  - `provider_sid?: string`
  - `created_at: ISO8601`
  - `updated_at: ISO8601`

## Notes
- Backend chosen via `FAX_BACKEND` env var.
- TXT files are converted to PDF before TIFF conversion.
- If Ghostscript is missing, TIFF step is stubbed with pages=1; install for production.
