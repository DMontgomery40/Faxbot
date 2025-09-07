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
  - `backend: string` ("phaxio", "sinch", or "sip")
  - `provider_sid?: string`
  - `created_at: ISO8601`
  - `updated_at: ISO8601`

## Notes
- Backend chosen via `FAX_BACKEND` env var: `phaxio` (cloud via Phaxio/Phaxio‑by‑Sinch V2 style), `sinch` (cloud via Sinch Fax API v3 direct upload), or `sip` (self‑hosted Asterisk).
- TXT files are converted to PDF before TIFF conversion.
- If Ghostscript is missing, TIFF step is stubbed with pages=1; install for production.
- For the `phaxio` backend, TIFF conversion is skipped; page count is finalized via the provider callback (`/phaxio-callback`, HMAC verification supported).
- For the `sinch` backend, the API uploads your PDF directly to Sinch. Webhook support is under evaluation; status reflects the provider’s immediate response and may be updated by polling in future versions.
- Tokenized PDF access has a TTL (`PDF_TOKEN_TTL_MINUTES`, default 60). The `/fax/{id}/pdf?token=...` link expires after TTL.
- Optional retention: enable automatic cleanup of artifacts by setting `ARTIFACT_TTL_DAYS>0` (default disabled). Cleanup runs every `CLEANUP_INTERVAL_MINUTES` (default 1440).

## Phone Numbers
- Preferred format: E.164 (e.g., `+15551234567`).
- Validation: API accepts `+` and 6–20 digits.
- Cloud path (Phaxio): the service may attempt best‑effort normalization for non‑E.164 input; provide E.164 to avoid ambiguity.

## Audit Logging (Optional)
- Enable structured audit logs for SIEM ingestion:
  - `AUDIT_LOG_ENABLED=true`
  - `AUDIT_LOG_FORMAT=json` (default)
  - `AUDIT_LOG_FILE=/var/log/faxbot_audit.log` (optional)
  - `AUDIT_LOG_SYSLOG=true` and `AUDIT_LOG_SYSLOG_ADDRESS=/dev/log` (optional)
- Events: `job_created`, `job_dispatch`, `job_updated`, `job_failed`, `pdf_served`.
- Logs contain job IDs and metadata only (no PHI).
