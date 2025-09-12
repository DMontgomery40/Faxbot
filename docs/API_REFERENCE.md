---
layout: default
title: API Reference
nav_order: 10
permalink: /api-reference.html
---

# API Reference

## Base URL
- Default: `http://localhost:8080`
- Health: `GET /health` → `{ "status": "ok" }`
 - Readiness: `GET /health/ready` → 200 when ready; 503 otherwise

## Authentication and API Keys
- Send `X-API-Key: <token>` on every request. Two options exist:
- Send `X-API-Key: <token>` on every request. Two options exist:
  - DB‑backed tokens (recommended): format `fbk_live_<keyId>_<secret>`, minted via admin endpoints.
  - Env bootstrap key (legacy): the literal `API_KEY` value configured on the server. Use this only to bootstrap and mint DB keys.
- Set `REQUIRE_API_KEY=true` in production/HIPAA to enforce authentication even if `API_KEY` is blank.

How to get an API key (recommended flow)
1) Set a temporary bootstrap admin key in the server environment: `API_KEY=bootstrap_admin_only` (or any strong value).
2) Create a DB-backed key via admin endpoint (requires admin auth with the bootstrap env key):
```
curl -s -X POST http://localhost:8080/admin/api-keys \
  -H "X-API-Key: $API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"name":"dev","owner":"you@example.com","scopes":["fax:send","fax:read"]}'
```
3) Save the returned `token` (`fbk_live_<keyId>_<secret>`) securely — it is shown ONCE.
4) Use that token as `X-API-Key` for all client calls. You can delete the bootstrap `API_KEY` later if you wish.

Scopes
- Enforced when authentication is required:
  - `POST /fax` → scope `fax:send`
  - `GET /fax/{id}` → scope `fax:read`
  - Inbound list/get/download → scopes `inbound:list` / `inbound:read`
  - Admin endpoints → `keys:manage` (or the bootstrap env key)
- If `REQUIRE_API_KEY=false` and no `API_KEY` is set, unauthenticated requests are allowed in development; scopes are then not required.

Token lifecycle (rotate, revoke, expire)
- Rotate: `POST /admin/api-keys/{keyId}/rotate` returns a new plaintext token once; the old secret stops working immediately.
- Revoke: `DELETE /admin/api-keys/{keyId}` sets `revoked_at` — the token is rejected thereafter.
- Expire: create keys with `expires_at` (ISO8601) to enforce automatic expiry.
- Metadata: `GET /admin/api-keys` lists non‑secret fields (`created_at`, `last_used_at`, `expires_at`, `revoked_at`, `scopes`, `owner`, `name`).

Error semantics
- 401 Unauthorized: missing/invalid token, revoked/expired token, or no admin auth for admin endpoints.
- 403 Forbidden: valid token but insufficient scopes for the endpoint.
- 429 Too Many Requests: per‑key rate limit exceeded.

### Rate limiting (optional)
- Global: `MAX_REQUESTS_PER_MINUTE` (default 0 = disabled). When enabled, each API key is limited per-minute across requests.
- Inbound-specific: `INBOUND_LIST_RPM` and `INBOUND_GET_RPM` apply to `GET /inbound`, `GET /inbound/{id}`, and `GET /inbound/{id}/pdf` (API-key path). Token-based downloads are not rate-limited.
- Exceeding the limit returns `429 Too Many Requests` with a `Retry-After` header.

## Endpoints

1) POST `/fax`
- Multipart form
  - `to`: destination number (E.164 or digits)
  - `file`: PDF or TXT
- Implementation details
  - Streaming upload: file data streams to disk in chunks; the 10 MB limit is enforced incrementally to avoid memory spikes.
  - Magic sniff: server validates content by signature — PDFs must begin with `%PDF`; TXT must be UTF‑8 decodable. Declared `content-type` is not trusted.
  
- Responses
  - 202 Accepted: `{ id, to, status, error?, pages?, backend, provider_sid?, created_at, updated_at }`
  - 400 bad number; 413 file too large; 415 unsupported type; 401 invalid API key; 403 missing `fax:send` scope
- Example
```
curl -X POST http://localhost:8080/fax \
  -H "X-API-Key: $API_KEY" \
  -F to=+15551234567 \
  -F file=@./example.pdf
```

2) GET `/fax/{id}`
- Returns job status as above.
- 404 if not found; 401 if invalid API key; 403 if missing `fax:read` scope.
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

5) Admin — API keys
- Requires admin auth: either the bootstrap env key (`API_KEY`) or a DB key with scope `keys:manage`.

  a) Create a key — `POST /admin/api-keys`
  - Body: `{ name?: string, owner?: string, scopes?: string[], expires_at?: ISO8601, note?: string }`
  - Returns: `{ key_id, token, name?, owner?, scopes[], expires_at? }` (token shown ONCE)
  - Example:
```
curl -s -X POST http://localhost:8080/admin/api-keys \
  -H "X-API-Key: $API_KEY" -H 'Content-Type: application/json' \
  -d '{"name":"svc-bot","owner":"svc@example.com","scopes":["fax:send"]}'
```

  b) List keys — `GET /admin/api-keys`
  - Returns: `[{ key_id, name?, owner?, scopes[], created_at, last_used_at?, expires_at?, revoked_at?, note? }]`

  c) Revoke key — `DELETE /admin/api-keys/{keyId}`
  - Response: `{ status: "ok" }`

  d) Rotate key — `POST /admin/api-keys/{keyId}/rotate`
  - Returns: `{ key_id, token }` (new token shown ONCE)

6) Admin — Config (read-only)
- `GET /admin/config`
  - Requires admin auth.
  - Returns a sanitized view of effective configuration and runtime flags (no secrets):
    - `backend`, `require_api_key`, `enforce_public_https`, `phaxio_verify_signature`, `audit_log_enabled`
    - `rate_limits` (global and inbound), `inbound` (enabled, retention, token TTL)
    - `storage` (backend, masked S3 identifiers), `backend_configured` booleans, `public_api_url`

7) Inbound (WIP scaffolding; enable with `INBOUND_ENABLED=true`)
 - `GET /inbound` (requires scope `inbound:list`) — list recent inbound faxes.
 - `GET /inbound/{id}` (requires scope `inbound:read`) — get metadata.
 - `GET /inbound/{id}/pdf` — download inbound PDF via either `?token=...` (short‑TTL) or API key with `inbound:read`.
 - Internal (SIP/Asterisk): `POST /_internal/asterisk/inbound` with header `X-Internal-Secret: <secret>` and body `{ tiff_path, to_number, from_number?, faxstatus?, faxpages?, uniqueid }`.

Retention and cleanup (inbound)
- Set `INBOUND_RETENTION_DAYS` to automatically delete stored inbound artifacts after the specified days. The cleanup loop runs every `CLEANUP_INTERVAL_MINUTES`.
- For S3 storage, objects are deleted via the API; for local storage, files are unlinked. Metadata rows remain for audit/history, but `pdf_path`/`tiff_path` are cleared.
- When `STORAGE_BACKEND=s3`, local PDFs are removed immediately after upload (only the S3 URI is retained).

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
- Default backend is `phaxio`. Set `FAX_BACKEND=sip` explicitly if you are running Asterisk; set `FAX_BACKEND=sinch` for Sinch v3 direct upload.
- TXT files are converted to PDF before TIFF conversion.
- If Ghostscript is missing, TIFF step is stubbed with pages=1; install for production.
- For the `phaxio` backend, TIFF conversion is skipped; page count is finalized via the provider callback (`/phaxio-callback`, HMAC verification supported).
- For the `sinch` backend, the API uploads your PDF directly to Sinch. Webhook support is under evaluation; status reflects the provider’s immediate response and may be updated by polling in future versions.
- Tokenized PDF access has a TTL (`PDF_TOKEN_TTL_MINUTES`, default 60). The `/fax/{id}/pdf?token=...` link expires after TTL.
- API key tokens are never logged; audits record only the `key_id`.
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
