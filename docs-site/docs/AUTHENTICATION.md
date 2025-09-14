---
layout: default
title: Authentication (API Keys)
parent: Security
nav_order: 2
permalink: /security/authentication.html
---

# Authentication (API Keys)

Faxbot authenticates requests using an `X-API-Key` header. There are two key types:

- DB‑backed keys (recommended): tokens of the form `fbk_live_<keyId>_<secret>` created via admin endpoints.
- Bootstrap env key (legacy): the literal `API_KEY` value set on the server; use only to bootstrap DB keys.

## Quick Start — Get a DB‑Backed Key

1) Set a temporary bootstrap admin key in the server environment (for example in `.env`):
```
API_KEY=bootstrap_admin_only
REQUIRE_API_KEY=true
```
2) Create a per‑user/service key via the admin endpoint (authenticate with the bootstrap `API_KEY`):
```
curl -s -X POST http://localhost:8080/admin/api-keys \
  -H "X-API-Key: $API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"name":"dev","owner":"you@example.com","scopes":["fax:send","fax:read"]}'
```
3) Save the returned `token` securely. It is shown once.
4) Use that token on all client requests:
```
curl -H "X-API-Key: fbk_live_<keyId>_<secret>" http://localhost:8080/health
```

## Scopes

Scopes limit what a key can do:
- `fax:send` — required for `POST /fax`
- `fax:read` — required for `GET /fax/{id}`
- `inbound:list` and `inbound:read` — required for inbound listing/metadata/download
- `keys:manage` — required for admin key management endpoints

If `REQUIRE_API_KEY=false` and no `API_KEY` is set, unauthenticated requests are allowed in development, and scopes are not enforced.

## Key Lifecycle

- Rotate — `POST /admin/api-keys/{keyId}/rotate` returns a new plaintext token once; the old secret is immediately invalid.
- Revoke — `DELETE /admin/api-keys/{keyId}` sets `revoked_at`, permanently disabling the token.
- Expire — when creating a key, set `expires_at` (ISO8601) to enforce automatic expiry.
- List metadata — `GET /admin/api-keys` returns non‑secret fields: `scopes`, timestamps, `owner`, `name`, `revoked_at`.

## Admin Endpoints (Summary)

- Create: `POST /admin/api-keys` → returns `{ key_id, token, ... }` (token shown once)
- List: `GET /admin/api-keys` → returns array of metadata (no secrets)
- Revoke: `DELETE /admin/api-keys/{keyId}` → `{ status: "ok" }`
- Rotate: `POST /admin/api-keys/{keyId}/rotate` → `{ key_id, token }` (new token shown once)

Admin auth: use either the bootstrap env `API_KEY`, or a DB key that has the `keys:manage` scope.

## Enforcement & Errors

- `REQUIRE_API_KEY=true` (recommended for HIPAA/production) enforces authentication even if `API_KEY` is blank.
- 401 Unauthorized — missing/invalid, revoked, or expired token; or no admin auth for admin endpoints.
- 403 Forbidden — valid token but insufficient scopes.
- 429 Too Many Requests — per‑key rate limit exceeded.

## Rate Limiting (Optional)

- Global per‑key: `MAX_REQUESTS_PER_MINUTE` (default 0 disables)
- Inbound per‑route: `INBOUND_LIST_RPM`, `INBOUND_GET_RPM` (token downloads are not rate‑limited)

## Security Tips

- Prefer DB‑backed keys per user/service; avoid sharing a single env key.
- Rotate regularly and remove unused keys.
- For public deployments, place the API behind TLS and a reverse proxy/WAF with additional rate limits and IP allowlists.

