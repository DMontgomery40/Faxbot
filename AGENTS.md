# AGENTS.md - Critical Instructions for AI Assistants

## 🚨 UNPRECEDENTED PROJECT WARNING 🚨

**THIS PROJECT HAS NEVER EXISTED BEFORE IN HISTORY**

There has **NEVER** been an open source, locally hostable fax server API with AI assistant integration. **EVER.** None of this exists in any AI training data. You **CANNOT** make assumptions about how this works, what patterns to follow, or what "normal" looks like.

**EVERY DECISION MUST BE BASED ON THE ACTUAL CODEBASE AND DOCUMENTATION PROVIDED**

## Project Identity: Faxbot

**Correct Name:** "Faxbot" (main project) and "faxbot-mcp" (MCP server)  
**NEVER:** "OpenFax", "twilio-fax", or any other name  
**Status:** Production deployment capable, handling PHI/PII in healthcare environments

## Revolutionary Architecture Overview (v3 Modular Plugins)

Faxbot is the first and only open source fax transmission system that combines:

1. **Modular Provider Plugins (v3)** — Outbound, inbound, auth, and storage provider slots are resolved at runtime via a single config file. Providers are implemented as plugins (Python for backend execution; Node plugins for MCP/UI helpers only).
2. **Multiple Backends via Plugins** — Cloud (Phaxio, Sinch), Self‑hosted (SIP/Asterisk), and Test Mode are expressed as plugins bound to provider slots.
3. **AI Assistant Integration** — Two MCP servers (Node + Python) with three transports each (stdio/HTTP/SSE) derive tools from active capabilities.
4. **Developer SDKs** — Node.js and Python with identical APIs (OpenAPI alignment), stable error mapping.
5. **HIPAA Compliance** — Built‑in controls for healthcare PHI handling across plugins (HMAC verification, secure storage options, no secret logging).
6. **Non‑Healthcare Flexibility** — Configurable security for non‑PHI scenarios; safe defaults remain available.

**Architecture Flow:**
```
AI Assistant (Claude) → MCP Server → Fax API → Backend → Fax Transmission
    ↓                      ↓            ↓         ↓
SDK Client          → Fax API → Backend → Fax Transmission
```

## Admin Console First (GUI Mandate)

Effective immediately, Faxbot’s north star is a complete, GUI-first experience through the Admin Console. Users should never need a terminal for routine operations beyond starting the Docker container/console.

What this means for all agents and contributors
- No CLI-only features: every capability must be operable from the Admin Console.
- UX completeness: every setting, diagnostic, and job view includes inline help and deep links to docs.
- Contextual help everywhere: tooltips, helper text, and “Learn more” links across all screens.
- Backend isolation in the UI: show provider-specific guidance only for the selected backend.
- Mobile-first polish: layouts, spacing, and controls must be legible and usable on phones.

v3 UI additions
- Plugins tab: manage active provider plugins with enable/disable toggles and schema‑driven config forms.
- Curated registry search: discover available plugins; remote install is disabled by default and requires explicit approval when enabled.
- Contextual help per active provider: tips and “Learn more” links are plugin‑specific; no cross‑backend leakage.
- Scripts & Tests: backend‑aware quick actions; no CLI required. Local‑only Terminal is also available (see Security notes).

Acceptance criteria (per screen or feature)
- Inline explanation for each field or control (short helper text or tooltip).
- At least one “Learn more” link to Faxbot docs; add external provider links when relevant.
- Validation and error states guide users to a fix (not just an error code).
- Jobs list/detail: failed jobs surface targeted troubleshooting links for the active backend.
- Safe defaults: HIPAA users get secure defaults; non‑PHI users get friction‑reduced defaults.
- Responsive behavior validated at common mobile breakpoints.

Linking standards (docs & third‑party)
- Prefer concise tooltips plus a “Learn more” link to the specific docs section.
- Internal: link to relevant page in the Faxbot Jekyll docs site.
- External: link to Phaxio/Sinch/Asterisk docs only when the backend requires it.
- Never mix backends: users must not see instructions for a backend they aren’t using.

Developer notes
- Treat “UI parity” as part of definition of done. A feature is not complete until it has Admin Console coverage with help links and sensible defaults.
- Keep copy short and plain; reserve deep detail for the docs site.
- Avoid logging sensitive data in UI or network tabs; surface IDs/metadata only.

## Provider Slots and Backends (v3)

In v3, backends are provided by plugins bound to provider slots via a single resolved config.

Provider slots (initial)
- outbound: sending faxes (capabilities: send, get_status)
- inbound: receiving/callback models (capabilities: list_inbound, get_inbound_pdf; cloud callbacks remain HTTP endpoints in core that delegate to plugin handlers)
- auth (optional): authentication/authorization helpers (e.g., OIDC validation)
- storage (optional): artifact storage adapters (e.g., S3)

Only one outbound provider is active at a time. UI must only show guidance for the currently selected provider.

### 1. Phaxio Backend (Cloud) - RECOMMENDED FOR MOST USERS
**When to use:** Healthcare and business users wanting simplicity  
**Configuration:** `FAX_BACKEND=phaxio`

**Key Characteristics:**
- **Zero telephony knowledge required**
- **5-minute setup time**
- **Automatic HIPAA compliance** with BAA available
- **Cost:** ~$0.07 per page
- **Phaxio handles:** T.38 protocol, carrier relationships, number provisioning
- **You provide:** PDF/TXT files, destination numbers
- **Security:** TLS 1.2, webhook HMAC verification, no storage when configured

**Critical Setup Steps for HIPAA:**
1. Create Phaxio account at https://www.phaxio.com
2. Disable document storage in Fax Settings
3. Enable two-factor authentication
4. Email compliance@phaxio.com for BAA (Business Associate Agreement)
5. Configure webhook HMAC signature verification
6. Use HTTPS for all webhook URLs

**Environment Variables:**
```env
FAX_BACKEND=phaxio
PHAXIO_API_KEY=your_api_key_from_console
PHAXIO_API_SECRET=your_api_secret_from_console
PHAXIO_CALLBACK_URL=https://yourdomain.com/phaxio-callback
PUBLIC_API_URL=https://yourdomain.com
PHAXIO_VERIFY_SIGNATURE=true
ENFORCE_PUBLIC_HTTPS=true
```

### 2. SIP/Asterisk Backend (Self-Hosted) - FOR TECHNICAL USERS
**When to use:** High-volume users, cost-conscious, full control required  
**Configuration:** `FAX_BACKEND=sip` (default backend is `phaxio`; set `sip` explicitly)

**Key Characteristics:**
- **Requires SIP trunk provider** (like Twilio Voice, Bandwidth, etc.)
- **T.38 protocol knowledge essential**
- **Complex networking** (port forwarding, NAT traversal)
- **Cost:** Only SIP trunk charges (varies by provider)
- **You handle:** Asterisk configuration, SIP trunk setup, T.38 negotiation
- **Security:** AMI credentials, SIP authentication, network isolation

**Technical Requirements:**
- SIP Trunk Provider supporting T.38 fax
- Static IP or DDNS for SIP registration
- Port forwarding: 5060 (SIP), 4000-4999 (UDPTL media)
- Asterisk Manager Interface (AMI) access
- Understanding of SIP/RTP/UDPTL protocols

**Environment Variables (API):**
```env
FAX_BACKEND=sip
ASTERISK_AMI_HOST=asterisk
ASTERISK_AMI_PORT=5038
ASTERISK_AMI_USERNAME=api
ASTERISK_AMI_PASSWORD=secure_password_not_changeme
# Optional local station ID presented by the fax stack
FAX_LOCAL_STATION_ID="My Faxbot"
```

Note: SIP trunk credentials (username/password/server) are configured in your Asterisk/FS gateway, not in the Faxbot API.

### 3. Sinch Fax API v3 (Cloud)
Use when you prefer the direct upload model (“Phaxio by Sinch” accounts).

```env
FAX_BACKEND=sinch
SINCH_PROJECT_ID=your_project_id
SINCH_API_KEY=...
SINCH_API_SECRET=...
# Optional region override
# SINCH_BASE_URL=https://us.fax.api.sinch.com/v3
```

### 4. SignalWire Fax (Cloud) — PREVIEW
Use when you operate within SignalWire and prefer their Fax APIs.

```env
FAX_BACKEND=signalwire
SIGNALWIRE_SPACE_URL=https://<space>.signalwire.com
SIGNALWIRE_PROJECT_ID=...
SIGNALWIRE_API_TOKEN=...
SIGNALWIRE_FAX_FROM_E164=+15551234567
# Optional outbound status callback
SIGNALWIRE_STATUS_CALLBACK_URL=https://yourdomain.com/signalwire-callback
# Optional webhook verification
SIGNALWIRE_WEBHOOK_SIGNING_KEY=...
```

### 5. FreeSWITCH (Self-Hosted) — PREVIEW
Programmatic originate via `fs_cli` on the API host or ESL integration.

```env
FAX_BACKEND=freeswitch
FREESWITCH_GATEWAY_NAME=gw_signalwire
```

- Internal result hook (maps to job update): `POST /_internal/freeswitch/outbound_result` with `X-Internal-Secret: <ASTERISK_INBOUND_SECRET>` and JSON `{ job_id, fax_status, fax_result_text?, fax_document_transferred_pages?, uuid? }`.

### 6. Test/Development Backend — FOR DEVELOPMENT ONLY
**When to use:** Development, testing, CI/CD pipelines  
**Configuration:** `FAX_DISABLED=true`

**Key Characteristics:**
- **No actual fax transmission**
- **Simulates all API responses**
- **File processing works** (PDF/TXT conversion)
- **Database operations normal**
- **All endpoints return success**

## MCP Integration (Node + Python)

Two MCP servers live in `node_mcp/` (Node) and `python_mcp/` (Python). Each supports stdio/HTTP/SSE.
Additionally, a Node WebSocket helper is available for convenience; it mirrors tool calls but is not a formal MCP WebSocket transport (SEP‑1288 is under discussion).

| Transport | Node entrypoint | Python entrypoint | Port | Auth |
|-----------|------------------|-------------------|------|------|
| stdio     | `src/servers/stdio.js` | `stdio_server.py` | N/A  | API key |
| HTTP      | `src/servers/http.js`  | (n/a)            | 3001 | API key |
| SSE       | `src/servers/sse.js`   | `server.py`      | 3002/3003 | OAuth2/JWT |
| WebSocket helper | `src/servers/ws.js` | (n/a)         | 3004 | API key |

Notes
- Legacy MCP servers under `api/` were removed. Do not reference `api/mcp_*.js`.
- Node HTTP/SSE JSON limit is 16 MB to account for base64 overhead; REST API still enforces 10 MB raw file size.
- The WebSocket helper mirrors tool calls for convenience and is not a formal MCP transport.
- Prefer stdio + `filePath` for desktop assistants.

Admin Console Terminal (local-only)
- WebSocket endpoint: `/admin/terminal` (admin auth required).
- Backend uses pexpect to provide a TTY inside the API container; same privileges as the service user.
- Gate UI access with `ENABLE_LOCAL_ADMIN=true`; avoid exposing through proxies.

## The Two SDKs: Node.js and Python

### Identical API Surface:
```javascript
// Node.js SDK
const FaxbotClient = require('faxbot');
const client = new FaxbotClient('http://localhost:8080', 'api_key');
await client.sendFax('+15551234567', '/path/to/document.pdf');
await client.getStatus(jobId);
await client.checkHealth();
```

```python
# Python SDK  
from faxbot import FaxbotClient
client = FaxbotClient('http://localhost:8080', 'api_key')
client.send_fax('+15551234567', '/path/to/document.pdf')
client.get_status(job_id)
client.check_health()
```

**Both SDKs:**
- Version 1.1.0 (synchronized releases)
- Support PDF and TXT files only
- Identical error handling (400/401/413/415/404)
- Optional API key authentication
- Built-in health checking
- **Do NOT** directly integrate with backends (Phaxio/Asterisk)
- **Always call** Faxbot REST API endpoints

OpenAPI alignment
- FastAPI serves OpenAPI at `/openapi.json`; treat it as the source of truth for REST endpoints.
- SDKs and Admin UI types should match the OpenAPI contracts; codegen is optional but server must not drift from spec.

## Auth and API Keys (Updated)

- Multi-key auth is implemented. Tokens follow `fbk_live_<keyId>_<secret>` and are passed as `X-API-Key`.
- Admin endpoints (bootstrap with env `API_KEY` or a key with `keys:manage`):
  - `POST /admin/api-keys` (returns token once), `GET /admin/api-keys`,
  - `DELETE /admin/api-keys/{keyId}`, `POST /admin/api-keys/{keyId}/rotate`.
- Scopes are enforced:
  - `POST /fax` → `fax:send`
  - `GET /fax/{id}` → `fax:read`
  - Inbound list/get → `inbound:list` / `inbound:read` (see below)
- Set `REQUIRE_API_KEY=true` for production; dev mode can allow unauth when disabled.
- Optional per-key rate limiting: `MAX_REQUESTS_PER_MINUTE` (global), plus inbound list/get rpm.

Helpers
- `scripts/smoke-auth.sh` — local auth smoke test (no running server needed).
- `scripts/run-uvicorn-dev.sh` — start FastAPI locally; accepts `PORT`.
- `scripts/curl-auth-demo.sh` — mints a key then sends a test fax.

## HIPAA vs Non-HIPAA Configurations

### For Healthcare Users (PHI Handling)
**Requirements:** HIPAA compliance mandatory
```env
# Strict security settings
API_KEY=required_secure_key_here
ENFORCE_PUBLIC_HTTPS=true
PHAXIO_VERIFY_SIGNATURE=true
AUDIT_LOG_ENABLED=true
# For Phaxio: BAA required, storage disabled
# For SIP: Strong AMI passwords, network isolation
```

### For Non-Healthcare Users (No PHI)
**Goal:** Reduce friction while maintaining reasonable security
```env  
# More relaxed for convenience
API_KEY=optional_but_recommended
ENFORCE_PUBLIC_HTTPS=false  # Allow HTTP in development
REQUIRE_MCP_OAUTH=false     # Allow non-OAuth MCP access
AUDIT_LOG_ENABLED=false     # Reduce logging overhead
```

**Critical Balance:** The codebase must serve both audiences without compromising either:
- Healthcare users get HIPAA-compliant defaults
- Non-healthcare users get usability-focused defaults
- Clear documentation about which settings affect compliance

## Inbound Receiving 

- Enable with `INBOUND_ENABLED=true`.
- SIP/Asterisk (internal): `POST /_internal/asterisk/inbound` with `X-Internal-Secret: <ASTERISK_INBOUND_SECRET>` and JSON `{ tiff_path, to_number, from_number?, faxstatus?, faxpages?, uniqueid }`.
- List/get/download:
  - `GET /inbound` (scope `inbound:list`), `GET /inbound/{id}` (scope `inbound:read`),
  - `GET /inbound/{id}/pdf` via `?token=...` (short TTL, default 60m) or API key with `inbound:read`.
- Cloud callbacks:
  - Phaxio: `POST /phaxio-inbound` with HMAC verification (env `PHAXIO_INBOUND_VERIFY_SIGNATURE=true`).
  - Sinch: `POST /sinch-inbound` supports Basic (`SINCH_INBOUND_BASIC_USER/PASS`) and/or HMAC (`SINCH_INBOUND_HMAC_SECRET`).
- Storage backend:
  - `STORAGE_BACKEND=local|s3`; S3 supports SSE‑KMS (`S3_KMS_KEY_ID`) and S3‑compatible endpoints (`S3_ENDPOINT_URL`, e.g., MinIO). Local for dev only.
- Retention / rate limits (defaults per decisions):
  - `INBOUND_RETENTION_DAYS=30`, `INBOUND_TOKEN_TTL_MINUTES=60`, `INBOUND_LIST_RPM=30`, `INBOUND_GET_RPM=60`.

Notes
- Backends remain isolated: no Phaxio details in SIP paths and vice versa.
- Idempotency for inbound callbacks uses DB uniqueness on `(provider_sid, event_type)`.

v3 plugin note
- Inbound cloud callbacks remain core HTTP endpoints (Phaxio/Sinch) that delegate to plugin handlers; signature verification stays in core and cannot be disabled by plugins.

### Admin Console coverage for inbound (UI goals)
- Toggle to enable inbound receiving with clear warnings on storage and PHI.
- Storage configuration UI (local vs S3/S3‑compatible) with KMS and endpoint hints.
- Tokenized PDF access controls with TTL selector and help text.
- Inbound list/detail views with paging, filters, and download links guarded by scope.
- Troubleshooting link surfaces for common provider events and signature verification.

## Key API Endpoints & Workflows

### Core REST API (main.py)
```
POST /fax              # Send fax (multipart: to, file)
GET  /fax/{id}         # Check fax status  
GET  /fax/{id}/pdf     # Tokenized PDF access (for cloud backends)
POST /phaxio-callback  # Phaxio webhook (status updates)
POST /signalwire-callback  # SignalWire status callback (optional HMAC verification)
GET  /health           # Service health check
```

### Admin Console surface area (must-haves)
- Settings: backend selection, auth, storage, tokens/TTLs, rate limits, HIPAA toggles.
- Diagnostics: health status, webhook signature checks, environment checks, limits.
- Jobs: queue status, progress, pages, failures with contextual remediation links.
- Keys: API key management (mint/list/rotate/delete) with copy-to-clipboard UX.
- Inbound (when enabled): listing, detail, secure download, retention status.
- Plugins (v3): list native + manifest providers; enable/disable outbound; schema‑driven config forms; curated registry search.
- Tools group: Terminal (local-only), Diagnostics, Logs, Scripts & Tests (backend-aware quick runs), Plugins.

### MCP Tools (v3 parity)
- send_fax
  - stdio: `{ to, filePath }` preferred; `{ to, fileContent, fileName, fileType }` supported
  - HTTP/SSE: `{ to, fileContent, fileName, fileType }` (base64 required)
- get_fax_status: `{ jobId }`
- list_inbound: `{ limit?, cursor? }` (when inbound enabled)
- get_inbound_pdf: `{ inboundId, asBase64? }` (guarded by scopes/limits)

No OCR tools (`faxbot_pdf` removed by design). Node and Python MCP servers must expose the same tool set for a given config.

Admin Actions (container checks)
- UI exposes an allowlisted set of safe container checks under Tools → Scripts & Tests.
- Endpoints: `GET /admin/actions` (list), `POST /admin/actions/run` (execute), admin-only.
- Enabled only for local admin (gated by `ENABLE_ADMIN_EXEC` and `ENABLE_LOCAL_ADMIN`). No arbitrary commands allowed.

### Typical Workflows

**Phaxio Workflow:**
1. Client sends fax → API validates → Creates job record
2. API generates secure PDF token → Calls Phaxio API with PDF URL
3. Phaxio fetches PDF → Transmits fax → Sends webhook callback
4. Callback updates job status → Client can check status

**SIP/Asterisk Workflow:**
1. Client sends fax → API validates → Converts PDF to TIFF
2. API calls Asterisk AMI → Originates fax call
3. Asterisk handles T.38 negotiation → Transmits fax
4. AMI event updates job status → Client can check status

## Critical Implementation Warnings

### 1. Backend Isolation is MANDATORY
With v3 plugins, isolation is enforced at the UI and config layer:
- Only the active outbound provider’s settings and guidance are shown.
- Switching providers is a guided flow; never show mixed provider setup on the same screen.

### 2. MCP File Handling — Practical Notes
- stdio: pass `filePath` (no base64, no token limits)
- HTTP/SSE: pass base64 in JSON; Node MCP limit 16 MB; REST API raw limit 10 MB.
- File types: PDF and TXT only. Convert images (PNG/JPG) to PDF first.

UI implications
- File pickers enforce PDF/TXT; show conversion hint for images.
- Enforce 10 MB raw limit client-side with clear pre-submit messaging.
- For HTTP/SSE MCP flows, surface the 16 MB JSON limit when base64 applies.

### 3. Authentication Layers
```
MCP Layer:     API_KEY or OAuth2/JWT (depending on transport)  
API Layer:     X-API-Key header (optional if blank)
Backend Layer: Provider-specific (Phaxio auth, AMI credentials)
```

### 4. Error Handling Consistency
All components must return identical HTTP error codes:
- **400:** Bad request (invalid phone, missing params)
- **401:** Authentication failed
- **404:** Job/resource not found  
- **413:** File too large (>10MB default)
- **415:** Unsupported file type (not PDF/TXT)

### 5. File Processing Pipeline (no OCR)
```
Upload → Validation → (TXT→PDF) → Backend-specific
Phaxio: tokenized PDF URL + HMAC callback
Sinch: direct upload
SIP: PDF→TIFF → AMI originate (T.38)
```

### 6. Database Model Understanding
```sql
fax_jobs:
  id (UUID)           # Job identifier
  to_number           # Destination (E.164)  
  status              # queued/in_progress/SUCCESS/FAILED
  backend             # phaxio/sip/disabled
  provider_sid        # Phaxio fax ID or AMI job ID
  pdf_url             # Public PDF URL (Phaxio only)
  pdf_token           # Secure access token (Phaxio only)
  pdf_token_expires   # Token TTL (Phaxio only)
  error               # Failure reason
  pages               # Page count when available
  created_at/updated_at
```

## Deployment Considerations

### Docker Compose Services
- **api:** Main FastAPI fax service (always required)
- **asterisk:** SIP/Asterisk backend (only for FAX_BACKEND=sip)  
- **faxbot-mcp:** MCP server (optional, for AI integration)

### Port Mappings
- **8080:** Main API service
- **3001:** MCP HTTP server (when enabled)
- **3002:** MCP SSE+OAuth server (when enabled)
- **5060:** SIP signaling (Asterisk backend only)
- **5038:** AMI interface (Asterisk only, **keep internal**)
- **4000-4999:** UDPTL media (Asterisk T.38 fax)

### Admin Console UX for deployment
- Provide copyable example `docker-compose.yml` and `.env` templates.
- Offer environment checklists with links to docs (TLS, webhooks, AMI security).
- Warn when public HTTPS enforcement is off and PHI is enabled.

### Volume Requirements
- **faxdata:** Persistent storage for PDFs, TIFFs, job artifacts
- **Database:** SQLite file or external database connection

### Production Architecture (for Agents)
- Prefer containerized API behind TLS (reverse proxy/WAF). Do not attempt to run the API as serverless functions; it needs binary deps and file handling.
- Database: use managed PostgreSQL; set `DATABASE_URL=postgresql+psycopg2://...`. SQLite is dev-only.
- Storage: use `STORAGE_BACKEND=s3` with SSE‑KMS (`S3_KMS_KEY_ID`) for PHI; S3‑compatible endpoints supported for on‑prem (MinIO).
- Asterisk (SIP): isolate in private networks; never expose AMI; open only required SIP/UDPTL ports to trunk provider IPs.
- Multi‑instance: app’s in‑memory rate limiting is per‑node; rely on edge rate limiting or add a distributed limiter later.

See also: `docs/DEPLOYMENT.md`.

## v3 Plugin Architecture — Contracts and Endpoints

Feature flags
- `FEATURE_V3_PLUGINS=true` enables plugin discovery endpoints and the Admin Console Plugins tab.
- `FEATURE_PLUGIN_INSTALL=false` by default; remote install is disabled unless explicitly approved and allowlisted.

Config store
- Single resolved config file at `config/faxbot.config.json` (override with `FAXBOT_CONFIG_PATH`).
- Structure: `{ version, providers: { outbound: { plugin, enabled, settings }, inbound: { ... }, auth?: { ... }, storage?: { ... } } }`
- Atomic writes with backups; rollback to last known‑good on validation/startup failure; surface Admin UI banner.

Discovery and endpoints (when `FEATURE_V3_PLUGINS=true`)
- `GET /plugins` — list installed plugins with manifests and current enabled/config values
- `GET /plugins/{id}/config` — return enabled + settings for a plugin
- `PUT /plugins/{id}/config` — validate via JSON Schema and persist atomically
- `GET /plugin-registry` — serve curated registry JSON for UI search

Manifest providers (HTTP) — preview
- Data-only providers are supported via a declarative manifest executed by core (no third‑party code in server).
- Runtime: `api/app/plugins/http_provider.py` interprets manifests with:
  - `auth` schemes: `basic|bearer|api_key_header|api_key_query|none`
  - `actions.send_fax|get_status`: method, url, headers, `body.kind` (`json|form|multipart|none`), `body.template`
  - Response mapping via simple JSONPath-like selectors (`data.id`, `data.list[0].field`) and optional `status_map`
  - Policy: `allowed_domains[]`, `timeout_ms`, redaction (follow-up), HTTPS only in HIPAA
- Storage: manifests are persisted under `FAXBOT_PROVIDERS_DIR` (default `config/providers/<id>/manifest.json`).
- New admin endpoints (feature-gated, admin-only):
  - `POST /admin/plugins/http/validate` — validate a manifest + optional dry-run send; returns normalized result
  - `POST /admin/plugins/http/install` — persist the manifest to the providers dir
- Resolution: when `FEATURE_V3_PLUGINS=true` and outbound plugin references an installed manifest id, core uses the manifest runtime for send/status.
- Security (HIPAA defaults): remote install disabled by default; enforce domain allowlists, strict timeouts/body caps; redact secrets; no arbitrary code.

Admin Console (planned builder)
- json manifests [cont]

Security and permissions
- New admin scopes: `admin:plugins:read`, `admin:plugins:write` for list/get/update.
- Only keys with `keys:manage` may change plugin configs.
- Per‑key RPM limits should mirror inbound list/get defaults for plugin reads; stricter for writes.

Dynamic install (optional, off by default)
- If enabled, enforce a strict allowlist with checksums (and signatures if provided); non‑interactive, sandboxed installs only.
- For HIPAA profiles, keep remote install disabled.

## Security Architecture Deep Dive

### Threat Model
1. **PHI Exposure:** Healthcare data in fax content
2. **Unauthorized Access:** API without authentication
3. **Network Eavesdropping:** Unencrypted transmission
4. **Callback Spoofing:** Fake webhook status updates
5. **Credential Compromise:** Weak or default passwords

### Security Controls by Component

**API Layer:**
- X-API-Key authentication (optional but recommended)
- HTTPS enforcement for public deployments  
- Request size limits (10MB default)
- File type validation (PDF/TXT only)
- Phone number validation (E.164 preferred)

**Phaxio Backend:**
- TLS 1.2 for all API calls
- HMAC-SHA256 webhook signature verification
- No document storage when properly configured
- BAA available for HIPAA compliance

**SIP/Asterisk Backend:**  
- AMI authentication required
- Network isolation recommended (VPN/firewall)
- Strong SIP trunk credentials
- T.38 encryption when available

**MCP Layer:**
- Multiple authentication options (API key, OAuth2/JWT)
- Transport-specific security (stdio vs HTTP vs SSE)
- No credential logging in development mode

### BAA and Subprocessor Matrix (for Agents)

If you operate Faxbot as a hosted service that processes, transmits, or stores PHI, you are a Business Associate. Ensure BAAs are in place with customers and all subprocessors that can access PHI.

Guidance
- Keep a living subprocessor inventory and data‑flow diagram.
- Execute BAAs with PHI‑touching vendors before onboarding healthcare customers.
- Align incident response and breach notifications with BAA timelines.

Typical subprocessor matrix

| Category | Example vendors | PHI exposure | BAA required | Notes |
|---|---|---|---|---|
| Cloud IaaS/PaaS | AWS/GCP/Azure | Possible | Yes | Use HIPAA‑eligible services; enforce TLS; least privilege. |
| Object storage | S3/S3‑compatible (KMS) | Yes | Yes | SSE‑KMS, bucket policies, lifecycle rules. |
| Database | Managed Postgres | Possible | Yes | Encrypt at rest, TLS in transit, backups with PITR. |
| Fax transport | Phaxio/Sinch | Yes | Yes | Verify HMAC/webhooks; disable provider storage if policy requires. |
| SIP trunk | Bandwidth/Twilio/etc. | Yes | Yes | T.38; restrict ports/IPs; keep AMI internal. |
| Logging/monitoring | SIEM/OTEL | Avoid PHI | Prefer BAA | Log IDs/metadata only; scrub content and numbers. |
| CDN/static site | Netlify/CloudFront | No PHI | No (for non‑PHI) | Don’t serve PHI via public CDN; disable analytics on HIPAA sites. |

Customer‑hosted vs you‑hosted
- Self‑hosted customers: typically no BAA with you if you don’t access PHI. Avoid support practices that expose PHI unless under a BAA.
- Hosted service (faxbot.net): BAAs with customers and all subprocessors are required.

### Security Headers (production)

Set at the edge (reverse proxy/WAF). PHI endpoints must send strict cache controls.

Required headers
- Strict‑Transport‑Security: `max-age=31536000; includeSubDomains; preload`
- Content‑Security‑Policy: tight allowlist; no inline scripts/styles
- X‑Content‑Type‑Options: `nosniff`
- Referrer‑Policy: `no-referrer`
- X‑Frame‑Options: `DENY` (or `SAMEORIGIN` as needed)
- Permissions‑Policy: disable unneeded features
- Cache‑Control (PHI endpoints): `no-store, no-cache, must-revalidate`
- Pragma: `no-cache`
- Expires: `0`
- CORS: restrict `Access-Control-Allow-Origin` to the UI domain; no `*` when credentials/PHI are involved

Example CSP
```
Content-Security-Policy: default-src 'self'; frame-ancestors 'none'; img-src 'self' data:; style-src 'self'; script-src 'self'; connect-src 'self' https://api.yourdomain.com; base-uri 'none'; form-action 'self';
```

Operational checks
- Enforce TLS; redirect HTTP→HTTPS.
- Validate webhooks (HMAC/signatures); IP allowlist if published by provider.
- Never log PHI; only IDs and generic metadata.

## Testing Strategy & Validation

### Backend Testing Matrix
```
Test Scenario          | Phaxio | Sinch | SignalWire | SIP/Asterisk | FreeSWITCH | Test Mode |
-----------------------|--------|-------|------------|--------------|------------|-----------|
PDF file upload        |   ✓    |   ✓   |     ✓      |      ✓       |     ✓*     |     ✓     |
TXT to PDF conversion  |   ✓    |   ✓   |     ✓      |      ✓       |     ✓*     |     ✓     |
Status checking        |   ✓    |   ✓   |     ✓      |      ✓       |     ✓*     |     ✓     |
Error handling         |   ✓    |   ✓   |     ✓      |      ✓       |     ✓*     |     ✓     |
Actual transmission    |   ✓    |   ✓   |     ✓      |      ✓       |     ✓*     |     ✗     |
Webhook callbacks      |   ✓    |   ✗   |     ✓      |      ✗       |     ✗      |     ✗     |
TIFF conversion        |   ✗    |   ✗   |     ✗      |      ✓       |     ✓*     |     ✗     |

*FreeSWITCH rows reflect current preview support with fs_cli/ESL integration and an internal result hook.
```

### MCP Testing Requirements -- test all with the OFFICIAL MCP INSPECTOR TOOL FROM ANTHROPIC
- **Stdio:** Test with Claude Desktop or Cursor configuration
- **HTTP:** Test session management and CORS handling
- **SSE+OAuth:** Test JWT validation and token expiration

### SDK Testing Requirements  
- **Cross-language consistency:** Node.js and Python identical behavior
- **Error code mapping:** Consistent HTTP status handling
- **Authentication:** Optional API key scenarios
- **Health checking:** Service availability detection

## Development Workflow and Open‑Core vs Commercial App

Q: If we plan to add a front end and inbound receiving while maintaining a lightweight MVP for users who only need to send faxes now, should we fork the repo into a separate commercial app and leave this one alone?

A: Short answer
- Don’t create a long‑lived fork of the core.
- Keep this repo as the canonical open‑core and continue adding generic capabilities (including receiving) here.
- Create a separate private repo (e.g., faxbot.net) for the commercial app that depends on the core via released packages/Docker images or a git submodule.

Why this is better
- Avoids divergence/merge‑hell: fixes and backend updates land once in core and flow into the app.
- Clear boundary: Core = protocol/backends/API/MCP/SDK; App = UI, billing, tenancy, provisioning, analytics.
- Faster iteration: The app ships UI/business features without destabilizing core.
- Compliance separation: PHI primitives stay consistent in core; plan/billing/tenant logic in the app.

What goes where
- Core (this repo)
  - REST API, validation, limits, HMAC verification
  - All backends (Phaxio/Sinch/SIP) send + receive plumbing
  - Webhook handlers (backend‑specific), file processing, tokenized file access, audit hooks
  - MCP servers (Node/Python) and SDKs (Node/Python)
- Commercial app (faxbot.net repo)
  - Frontend/dashboard (auth, orgs/teams, multi‑tenant)
  - Billing/plan limits, quotas, rate limiting, retention windows
  - Number provisioning UX, per‑tenant settings
  - Inbox UI, tagging/routing, notifications
  - Analytics, exports, support tooling

## Branch Policy (v3) - CRITICAL FOR AGENTS

### Branch Structure
- **`main`**: Production releases only. **AGENTS MUST NEVER WORK DIRECTLY IN MAIN.**
- **`development`**: Default branch for general core development work.
- **`docs-jekyll-site`**: GitHub Pages documentation branch.
- **App-specific branches**: For platform-specific applications (e.g., `electron_macos`, `electron_windows`, `electron_linux`, `iOS`).

### Agent Work Rules
1. **NEVER work in `main`** - This is for production releases only.
2. **General core work**: Use `development` branch.
3. **App-specific work**: Use the dedicated app branch:
   - Electron macOS work → `electron_macos` branch
   - Electron Windows work → `electron_windows` branch  
   - Electron Linux work → `electron_linux` branch
   - iOS app work → `iOS` branch
4. **Feature branches**: Only with owner approval, must merge back to appropriate target branch via PR.

### Branch Selection Logic for Agents
```
If working on Electron macOS → electron_macos
If working on Electron Windows → electron_windows  
If working on Electron Linux → electron_linux
If working on iOS app → iOS
If working on core API/MCP/docs → development
NEVER work in main
```

### Release Process
- Tag releases from `main` (e.g., `v3.0.0`) so consumers can pin stable versions.
- App branches merge to `development`, then `development` merges to `main` for releases.

Docs publishing
- GitHub Pages publishes from the `docs-jekyll-site` branch. Do not repoint Pages without approval.
- Author and iterate docs on `development`; promote to `docs-jekyll-site` when stable.
- Reference docs from the Admin Console using stable URLs only; avoid linking to WIP drafts.
- Keep backend‑specific pages separated (Phaxio vs Sinch vs SIP/Asterisk).
- Admin Console must derive all internal doc links from a single base (`DOCS_BASE_URL`), exposed at `/admin/config` as `branding.docs_base`. Never hard‑code full docs URLs in UI code.

Receiving capability recommendation
- Implement inbound fax support in core:
  - Phaxio/Sinch: inbound webhook endpoints with signature verification; tokenized access; backend isolation.
  - SIP/Asterisk: dialplan/AMI/AGI handler for T.38; TIFF→PDF conversion; storage and list/detail endpoints.
- Keep backend docs strictly separated per this AGENTS.md.

## Common Pitfalls & Anti-Patterns

### ❌ Don't Do This:
1. **Mix backend documentation** - Phaxio users see SIP instructions
2. **Assume MCP knowledge** - Explain it's for AI tool integration  
3. **Hard‑code backends** - In v3, always resolve through the provider adapter/config store; do not hard‑code backends or mix provider logic.
4. **Skip authentication** - Even non-HIPAA users need reasonable security
5. **Log PHI content** - PDF contents, phone numbers in production logs
6. **Default weak passwords** - "changeme" must be changed
7. **Expose AMI publicly** - Port 5038 should be internal only
8. **Skip HTTPS in production** - PHI requires encryption in transit

### ✅ Do This Instead:
1. **Backend-specific docs** - Clear separation of concerns
2. **Explain MCP context** - "For AI assistant tool integration"
3. **Dynamic backend loading** - Runtime configuration switching
4. **Graduated security** - Options for different compliance needs
5. **PHI-safe logging** - Redact sensitive information
6. **Secure defaults** - Require explicit configuration of credentials
7. **Network isolation** - Document proper AMI security
8. **TLS everywhere** - HTTPS for PHI, HTTP allowed for dev only
9. **Admin Console parity** - No feature considered done without GUI coverage
10. **Help everywhere** - Tooltips + “Learn more” links on every setting and error state

## Project Uniqueness Verification

**Research Conducted (December 2024):**
- **Open Source Fax Servers:** ICTFax, HylaFAX+, Asterisk modules exist
- **API-First Design:** None found with modern REST API
- **AI Integration:** No MCP servers for fax transmission found
- **Healthcare Focus:** Some HIPAA-compliant options, but complex enterprise only
- **Developer SDKs:** No standardized Node.js/Python client libraries
- **Multi-Backend:** No systems supporting both cloud and self-hosted options

**Conclusion:** Faxbot is genuinely unprecedented. Agents cannot rely on existing patterns or common solutions. Every decision must be based on the actual codebase architecture and requirements.

## Success Metrics & User Goals

### Non-Technical Healthcare User (Phaxio)
**Goal:** Send prescription to pharmacy in under 5 minutes
**Path:** Sign up → Configure → Send fax  
**Success:** Never sees SIP, Asterisk, or T.38 terminology

### Technical User (SIP/Asterisk)
**Goal:** Replace expensive fax service with self-hosted solution
**Path:** SIP trunk → Asterisk setup → Network configuration
**Success:** Understands T.38 requirements and cost implications  

### AI Enthusiast (MCP)
**Goal:** "Hey Claude, fax my insurance card to the doctor"
**Path:** MCP setup → Desktop AI configuration → Voice command
**Success:** Understands base64 limitation but sees future potential

### Developer (SDKs)
**Goal:** Integrate fax capability into existing application
**Path:** npm install faxbot → API integration → Error handling
**Success:** Identical experience across Node.js and Python

## Final Critical Reminders

1. **This has never existed before** - No assumptions allowed
2. **Multiple backends** - Cloud (Phaxio, Sinch, SignalWire), self‑hosted (SIP/Asterisk, FreeSWITCH), and Test mode are supported. Keep docs and UI strictly backend‑specific.
3. **Six MCP configurations** - 2 servers × 3 transports each
4. **HIPAA is not optional** - For healthcare users, compliance is mandatory
5. **Non-HIPAA users matter too** - Don't make everything enterprise-complex
6. **Project name is "Faxbot"** - Never OpenFax, never any other name
7. **Phaxio implementation is complete** - It's not a TODO anymore
8. **AMI security is critical** - Port 5038 must never be public
9. **OAuth2 can be optional** - For non-PHI scenarios
10. **Documentation must be backend-specific** - No mixed instructions

**Remember:** You are documenting a revolutionary system that bridges healthcare compliance requirements with modern AI assistant capabilities. No one has done this before. Your documentation could define how this category of software is understood for years to come.
