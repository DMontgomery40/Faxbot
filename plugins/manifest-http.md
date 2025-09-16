---
layout: default
title: HTTP Manifest Providers
parent: Plugins
nav_order: 3
permalink: /plugins/manifest-http.html
---

# HTTP Manifest Providers (Outbound)

Faxbot supports a lightweight outbound provider type defined by a JSON manifest. This lets you integrate REST fax APIs without writing server code.

Concepts
- Manifest: describes `send_fax` and optional `get_status`/`cancel_fax` actions
- Runtime: interprets the manifest, applies auth, renders bodies, and maps responses
- Allowed domains: explicit host allowlist per manifest to reduce SSRF risk

Manifest schema (simplified)
- `id`: provider ID (e.g., `acmefax`)
- `name`: display name
- `auth`: `{ scheme: none|basic|bearer|api_key_header|api_key_query, ... }`
- `allowed_domains`: array of hostnames
- `timeout_ms`: request timeout
- `actions`:
  - `send_fax`: `{ method, url, headers, body: { kind: json|form|multipart|none, template }, path_params: [], response: { job_id, status, error?, status_map? } }`
  - `get_status` (optional): `{ ... }`

Templates
- Simple mustache‑style `{{ var }}` interpolation against a context with `to`, `from`, `file_url`, `file_path`, `settings`, and `creds`

Multipart example
```
# body.kind = "multipart"
# body.template: request form fields, special key 'attachment'/'file' carries the PDF part
request={"to":[{"phoneNumber":"{{to}}"}]}&attachment={{file}}
```

Response mapping
- `response.job_id`: JSON path to the provider’s job ID (e.g., `data.id`)
- `response.status`: JSON path to status (e.g., `status`)
- `response.status_map`: optional map to normalize provider values to `queued|in_progress|SUCCESS|FAILED`
- `response.error`: optional JSON path for human‑readable error

Security
- Every manifest must include an `allowed_domains` array; requests to other hosts are blocked
- Secrets are not stored in the manifest; provide credentials via Admin Console or environment

Install & configure
1. Place `manifest.json` under `config/providers/<id>/manifest.json`
2. Enable v3 plugins: `FEATURE_V3_PLUGINS=true`
3. Persist selection via Admin Console → Plugins, or `PUT /plugins/{id}/config`
4. Apply credentials in Settings and test with the console’s Send tab

Admin API helpers
- `POST /admin/plugins/http/install` — install a single manifest (body or URL)
- `POST /admin/plugins/http/import-manifests` — bulk import manifests from a JSON file or URL
- `POST /admin/plugins/http/validate` — validate a manifest without installing

Notes
- Manifests are evaluated server‑side; no UI or browser secrets are required
- For HIPAA, disable dynamic install and keep manifests under version control
- The server also discovers manifests under `config/providers/<id>/manifest.json`
