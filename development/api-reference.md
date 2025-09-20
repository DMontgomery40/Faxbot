---
layout: default
title: API Reference
parent: Development
nav_order: 1
permalink: /development/api-reference.html
---

# API Reference

This page reflects the live API surface. Examples include cURL and the two official SDKs (Node.js and Python). Response bodies are shown and can be expanded per endpoint.

## Base URL
- Default: `http://localhost:8080`
- Health: `GET /health` → `{ "status": "ok" }`
- Readiness: `GET /health/ready` → checks DB, backend config, storage (when inbound enabled), Ghostscript

## Auth
- Header `X-API-Key: <token>` when API auth is required. For local admin testing, `bootstrap_admin_only` is accepted where enabled.
- Multi‑key support with scopes and per‑key rate limiting

## Core (outbound)

POST `/fax`
- Multipart form
  - `to`: destination number (E.164 or digits)
  - `file`: PDF or TXT
- Responses
  - 202 Accepted: `{ id, to, status, error?, pages?, backend, provider_sid?, created_at, updated_at }`
  - 400 invalid number/params; 413 too large; 415 unsupported type; 401 invalid API key

Examples

Language: cURL
```bash
curl -sS -X POST \
  -H "X-API-Key: $API_KEY" \
  -F to="+15551234567" \
  -F file=@/path/to/file.pdf \
  http://localhost:8080/fax
```

Language: Node.js SDK
```js
const FaxbotClient = require('faxbot');
const client = new FaxbotClient('http://localhost:8080', process.env.API_KEY);
const job = await client.sendFax('+15551234567', '/path/to/file.pdf');
console.log(job);
```

Language: Python SDK
```python
from faxbot import FaxbotClient
client = FaxbotClient('http://localhost:8080', os.environ.get('API_KEY'))
job = client.send_fax('+15551234567', '/path/to/file.pdf')
print(job)
```

<details>
<summary>Example 202 Response</summary>

```json
{
  "id": "6034835ea0e84528af720eabb147cb7d",
  "to": "+15553513514",
  "status": "in_progress",
  "error": null,
  "pages": null,
  "backend": "sinch",
  "provider_sid": null,
  "created_at": "2025-09-20T06:57:01.215761",
  "updated_at": "2025-09-20T06:57:02.274263"
}
```
</details>

GET `/fax/{id}`
- Returns job status as above
- 404 if not found; 401 if invalid API key

Language: cURL
```bash
curl -sS -H "X-API-Key: $API_KEY" \
  http://localhost:8080/fax/083fe9ce0b2e440e85aca37681830caa
```

<details>
<summary>Example 200 Response</summary>

```json
{
  "id": "083fe9ce0b2e440e85aca37681830caa",
  "to": "+15551234567",
  "status": "failed",
  "error": "Sinch create fax error 422: {\"code\":422,\"status\":\"INVALID_ARGUMENT\",\"message\":\"Unprocessable Entity\",\"details\":[{\"message\":\"Bad Request\",\"fieldViolations\":[{\"field\":\"To\",\"description\":\"To must be a valid phone number\"}]}]}",
  "pages": null,
  "backend": "sinch",
  "provider_sid": null,
  "created_at": "2025-09-20T07:30:38.131946",
  "updated_at": "2025-09-20T07:30:38.609095"
}
```
</details>

GET `/fax/{id}/pdf?token=...`
- Serves the original PDF to cloud providers
- No API auth; requires short‑TTL token
- 403 invalid/expired token; 404 not found

POST `/phaxio-callback`
- Phaxio status webhooks (form data)
- Correlate by query param `?job_id=...`
- If `PHAXIO_VERIFY_SIGNATURE=true`, verify `X-Phaxio-Signature` (HMAC‑SHA256 over raw body using `PHAXIO_API_SECRET`)

## Inbound (when enabled)
- `POST /phaxio-inbound` — HMAC verification optional (enabled by default via `PHAXIO_INBOUND_VERIFY_SIGNATURE`)
- `POST /sinch-inbound` — Basic and/or HMAC verification supported
- `POST /_internal/asterisk/inbound` — Internal Asterisk hook (`X-Internal-Secret: <ASTERISK_INBOUND_SECRET>`) with JSON `{ tiff_path, to_number, from_number?, faxstatus?, faxpages?, uniqueid }`
- `GET /inbound` — List inbound faxes (scope `inbound:list`)
- `GET /inbound/{id}` — Get inbound metadata (scope `inbound:read`)
- `GET /inbound/{id}/pdf?token=...` — Short‑TTL tokenized PDF access (or API key with `inbound:read`)

## Admin

Settings & diagnostics
- `GET /admin/settings` — Effective settings snapshot (sanitized)
- `POST /admin/settings/validate` — Check provider credentials/connectivity (non‑destructive)
- `PUT /admin/settings` — Apply settings in process (persist to environment variables)
- `POST /admin/settings/reload` — Reload settings from current environment
- `GET /admin/health-status` — Aggregated health state for Admin Console
- `GET /admin/db-status` — DB status (counts, connectivity)
- `GET /admin/logs` — Recent audit logs; `GET /admin/logs/tail` — stream subset
- `POST /admin/diagnostics/run` — One‑shot diagnostics bundle
- `POST /admin/restart` — Controlled process exit (requires `ADMIN_ALLOW_RESTART=true`)
- `GET /admin/settings/export` — Export current settings as `.env` snippet
- `POST /admin/settings/persist` — Save `.env` to server (`/faxdata/faxbot.env`) when enabled

API keys
- `POST /admin/api-keys` — Mint a key (returns token once)
- `GET /admin/api-keys` — List keys (metadata only)
- `DELETE /admin/api-keys/{keyId}` — Revoke
- `POST /admin/api-keys/{keyId}/rotate` — Rotate (returns new token once)

Jobs (admin)
- `GET /admin/fax-jobs` — Filterable list with masked numbers
- `GET /admin/fax-jobs/{id}` — Job detail (admin view)
- `GET /admin/fax-jobs/{id}/pdf` — Download outbound PDF (admin‑only)
- `POST /admin/fax-jobs/{id}/refresh` — Poll provider for status (enabled only for specific backends/manifests)

```bash
curl -sS -X POST -H "X-API-Key: $API_KEY" \
  http://localhost:8080/admin/fax-jobs/6034835ea0e84528af720eabb147cb7d/refresh
```

<details>
<summary>Example 400 (Sinch backend)</summary>

```json
{ "detail": "Refresh not supported for this backend" }
```
</details>

Plugins (feature‑gated)
- `GET /plugins` — List installed plugins (providers and storage)
- `GET /plugins/{id}/config` — Current `enabled` + `settings`
- `PUT /plugins/{id}/config` — Persist via config store (`FAXBOT_CONFIG_PATH`)
- `GET /plugin-registry` — Curated registry (file‑backed if present)

## Models
- FaxJobOut
  - `id: string`
  - `to: string`
  - `status: string` (queued | in_progress | SUCCESS | FAILED | disabled)
  - `error?: string`
  - `pages?: number`
  - `backend: string` (phaxio | sinch | sip | signalwire | freeswitch | manifest id)
  - `provider_sid?: string`
  - `created_at: ISO8601`
  - `updated_at: ISO8601`

## Notes
- Backend selection: `FAX_BACKEND=phaxio|sinch|sip|signalwire|freeswitch|<manifest id>`
- File limits: API raw upload limit defaults to 10 MB; MCP HTTP/SSE JSON limit is 16 MB (base64 overhead)
- TXT uploads are converted to PDF; SIP path converts PDF→TIFF before dialing
- For Phaxio, page count finalizes via webhook callbacks
- Tokenized PDF access TTL: `PDF_TOKEN_TTL_MINUTES` (default 60)
- Artifact cleanup: set `ARTIFACT_TTL_DAYS>0` (cleanup runs every `CLEANUP_INTERVAL_MINUTES`)

## Phone Numbers
- Preferred: E.164 (e.g., `+15551234567`)
- Validation: `+` and 6–20 digits

## Audit Logging (Optional)
- Enable structured audit logs: `AUDIT_LOG_ENABLED=true` (JSON format by default)
- Optional sinks: file path, syslog (`AUDIT_LOG_SYSLOG=true`)
- Events include job lifecycle and admin activity; PHI content is never logged
