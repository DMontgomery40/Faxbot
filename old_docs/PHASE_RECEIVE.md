# Faxbot Inbound Receiving — Production-Grade Plan (Agent Implementation Guide)

Status: WIP/Draft — Lives on development branch. Do not publish or rely on as final until merged to main. Sections and interfaces may change.

This is a detailed, step-by-step build plan to add inbound fax receiving to Faxbot for all supported backends (Phaxio, Sinch, SIP/Asterisk, Test) in a HIPAA-aware, production-ready way. It’s written for a junior developer or implementation agent. Follow steps exactly; keep code changes minimal, secure-by-default, and strictly backend-isolated.

Important: You do NOT integrate with random services directly; you receive faxes because you control DIDs (phone numbers) reachable from the global PSTN. Cloud providers (Phaxio/Sinch) and SIP/Asterisk handle T.30/T.38 negotiations from any sender (fax machines, email-to-fax gateways, online services). Your job is to safely ingest, store, route, and expose inbound faxes via the Faxbot API.


## Ground Rules

- Project name is “Faxbot” (MCP server is “faxbot-mcp”). Do not rename anything.
- Backend isolation is mandatory. Phaxio instructions must not leak into SIP/Asterisk sections, and vice versa.
- HIPAA-first defaults for healthcare; relaxed dev defaults for non-PHI. No PHI in logs.
- Supported files remain PDF/TIFF (no OCR). TXT is irrelevant for inbound; do not add OCR.
- Preserve existing REST API behaviors and error codes. Only add new endpoints/DB models.
- Enforce HTTPS for public endpoints where configured. Verify provider signatures authenticating callbacks.


## Outcomes (Success Criteria)

- Accept inbound faxes across all backends. For cloud backends: verify signed callbacks; fetch PDFs. For SIP/Asterisk: ReceiveFAX generates TIFF → convert to PDF.
- Store artifacts securely with tokenized, time-limited retrieval. Enforce retention and cleanup. Compute SHA256 for integrity.
- Provide mailbox routing (DID → mailbox/department) and RBAC via API key scopes.
- Full audit: inbound received, accessed, deleted. No PHI in logs.
- SDK parity: list inbound, get inbound, download inbound PDF. MCP: metadata-only tools.


## Decision Point Index (flagged choices you must confirm)

- [DP-1] Storage backend for inbound PDFs: local encrypted volume vs object storage (e.g., S3 with SSE-KMS under BAA).
- [DP-2] Token TTL for inbound PDF downloads (minutes/hours); different TTLs for provider fetch vs user download.
- [DP-3] Retention duration (days) and whether to archive before deletion.
- [DP-4] Idempotency strategy for callbacks (key format, window, and dedupe store).
- [DP-5] Rate limiting per API key for inbound list/get/download.
- [DP-6] Mailbox routing model: simple DID→mailbox map vs rules engine (regex/multiple criteria).
- [DP-7] App-level encryption of PDFs (in addition to disk encryption): enable now or schedule for later.
- [DP-8] Asterisk integration signal: AMI UserEvent vs AGI HTTP post. Which do you prefer for your infra?
- [DP-9] Internal secret transport for Asterisk→API: header name, rotation process, and network scope.
- [DP-10] SDK download behavior: return tokenized URL vs stream file via SDK; both have tradeoffs.
- [DP-11] Logging and metrics platform: where to emit metrics/audits (stdout vs syslog vs OTEL collector).
- [DP-12] Scaling: single-process FastAPI background tasks vs external workers (RQ/Celery) for conversions.

Each DP is referenced in steps below. If unsure, pick the simplest secure option and leave a TODO with the DP tag for later refinement.


## Architecture Overview

Provider → Callback/Transport → Faxbot API → Storage + DB → Retrieval/Audit

- Phaxio/Sinch: Provider posts a signed callback to your HTTPS endpoint. You verify signature, create inbound record, fetch PDF from provider API (TLS), store file, and generate a short-lived token for retrieval.
- SIP/Asterisk: Your SIP trunk delivers the call to Asterisk; dialplan executes ReceiveFAX to generate TIFF. Asterisk notifies Faxbot (internal endpoint) with path + metadata; Faxbot converts TIFF→PDF, stores file, and creates inbound record.
- Test: Synthetic endpoint creates inbound faxes for dev/CI.


## Implementation Plan (Do These In Order)

### 0) Prep and Guardrails

- Branch: work on `development` (already created).
- Do not change or break existing outbound logic or endpoints.
- Keep new inbound endpoints namespaced and minimal.
- Reuse `audit_event()` for all security-relevant events (no PHI).
- Respect error codes: 400/401/403/404/413/415.


### 1) Config Additions (api/app/config.py)

Add new settings with sensible defaults:

- `inbound_enabled: bool` from `INBOUND_ENABLED` (default false)
- `inbound_retention_days: int` from `INBOUND_RETENTION_DAYS` (default 0 = no auto-delete)
- `inbound_max_pdf_mb: int` from `INBOUND_MAX_PDF_MB` (default 25)
- `inbound_token_ttl_minutes: int` from `INBOUND_TOKEN_TTL_MINUTES` (default 60)
- `asterisk_inbound_secret: str` from `ASTERISK_INBOUND_SECRET` (no default; required if SIP inbound enabled)
- `phaxio_inbound_verify_signature: bool` from `PHAXIO_INBOUND_VERIFY_SIGNATURE` (default true in prod)
- `sinch_inbound_verify_signature: bool` from `SINCH_INBOUND_VERIFY_SIGNATURE` (default true in prod)
- `storage_backend: str` from `STORAGE_BACKEND` (values: `local` or `s3`; default `local`) [DP-1]
- If `s3`: `S3_BUCKET`, `S3_PREFIX` (e.g., `inbound/`), `S3_REGION`, and KMS options [DP-1]

Note: Keep non-HIPAA dev defaults permissive. For HIPAA, require HTTPS (`ENFORCE_PUBLIC_HTTPS=true`) and verification flags on.


### 2) Database Models (api/app/db.py)

Add new models (keep columns minimal at first, expandable later):

```python
class InboundFax(Base):
    __tablename__ = "inbound_faxes"
    id = Column(String(40), primary_key=True, index=True)  # UUID hex
    from_number = Column(String(64), index=True, nullable=True)
    to_number = Column(String(64), index=True, nullable=True)
    status = Column(String(32), index=True, nullable=False, default="received")  # received/failed
    backend = Column(String(20), nullable=False)
    provider_sid = Column(String(100), nullable=True)  # provider fax ID or call ID
    pages = Column(Integer, nullable=True)
    size_bytes = Column(Integer, nullable=True)
    sha256 = Column(String(64), nullable=True)
    pdf_path = Column(String(512), nullable=True)     # local path or s3 key
    tiff_path = Column(String(512), nullable=True)    # only for SIP path
    mailbox_label = Column(String(100), nullable=True)
    retention_until = Column(DateTime, nullable=True)
    pdf_token = Column(String(128), nullable=True)
    pdf_token_expires_at = Column(DateTime, nullable=True)
    error = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    received_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, nullable=False)

class Mailbox(Base):
    __tablename__ = "mailboxes"
    id = Column(String(40), primary_key=True, index=True)  # UUID hex
    label = Column(String(100), unique=True, nullable=False)
    allowed_scopes = Column(String(200), nullable=True)   # CSV (e.g., inbound:read:pharmacy)
    note = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, nullable=False)

class InboundRule(Base):
    __tablename__ = "inbound_rules"
    id = Column(String(40), primary_key=True, index=True)
    to_number = Column(String(64), index=True, nullable=False)
    mailbox_label = Column(String(100), nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
```

Migration notes:
- Use `Base.metadata.create_all(engine)` plus optional `PRAGMA table_info` checks (like existing ad-hoc migration) to add new columns if needed later.

[DP-6] Start with simple mapping `to_number → mailbox_label` via `InboundRule`. Expand to a rules engine later if needed.


### 3) Storage Layout (api/app)

- Local backend: store inbound under `FAX_DATA_DIR/inbound/` with files named `<id>.pdf` and `<id>.tiff` (if applicable). Ensure directory creation and permissions.
- S3 backend (optional): write PDFs to `s3://S3_BUCKET/S3_PREFIX/<id>.pdf` using server-side encryption (SSE-KMS). Store the key in `InboundFax.pdf_path`. Delete the local PDF after a successful upload so only the S3 URI remains. [DP-1]
- Always compute SHA256 of the final PDF for integrity; store in `sha256`.

[DP-1] Choose storage backend now. For HIPAA and scale, S3 with SSE-KMS (with BAA) is typical; for dev/single-node, local is fine.


### 4) Security Core

- HTTPS enforcement: if `ENFORCE_PUBLIC_HTTPS=true` and endpoint is public-facing, reject non-HTTPS `PUBLIC_API_URL`.
- Callback verification:
  - Phaxio: verify `X-Phaxio-Signature` HMAC-SHA256 over the raw body with `PHAXIO_API_SECRET` (already used outbound; mirror here).
  - Sinch: verify per Sinch’s inbound verification (e.g., Basic Auth or HMAC; implement configurable verification) — keep provider-specific logic contained.
- Internal Asterisk endpoint: require `X-Internal-Secret: <ASTERISK_INBOUND_SECRET>` on `POST /_internal/asterisk/inbound`. Reject any external traffic; recommend binding/internal network only.

[DP-9] Secret header naming and rotation policy.


### 5) Endpoints to Add (FastAPI)

Inbound metadata and retrieval:
- `GET /inbound` — list inbound faxes (filters: `to_number`, `since`, `status`, `mailbox`)
- `GET /inbound/{id}` — metadata for one inbound fax
- `GET /inbound/{id}/pdf?token=...` — tokenized PDF access (no API key if token matches and not expired)
- `GET /inbound/{id}/pdf` with `X-API-Key` and proper scope as an alternative to token-based access (optional)

Provider callbacks (public):
- `POST /phaxio-inbound` — verify signature; process event; fetch PDF
- `POST /sinch-inbound` — verify provider auth; process event; fetch PDF

Internal integration (non-public):
- `POST /_internal/asterisk/inbound` — body includes metadata and a path to TIFF; protected by internal secret

Admin routing:
- `POST /admin/mailboxes` — create
- `GET /admin/mailboxes` — list
- `PUT /admin/mailboxes/{id}` — update
- `POST /admin/inbound/rules` — create DID rule (to_number → mailbox_label)
- `GET /admin/inbound/rules` — list

Auth and scopes:
- Use the DB-backed API keys you will implement in PHASE_2.md plan:
  - `inbound:list` for `GET /inbound`
  - `inbound:read` for `GET /inbound/{id}` and `GET /inbound/{id}/pdf` (API-key variant)
  - `mailboxes:manage` for admin endpoints

[DP-5] Decide rate limits for list/get/pdf per key (see Section 11).


### 6) Phaxio Inbound Flow (Cloud Backend)

1. Configure Phaxio DID(s) to send inbound webhooks to your HTTPS endpoint: `/phaxio-inbound`.
2. In `POST /phaxio-inbound`:
   - Read raw body; verify `X-Phaxio-Signature` with `PHAXIO_API_SECRET`.
   - Parse payload (form or JSON). Extract: provider fax ID, to_number (DID), from_number, status.
   - Implement idempotency using `provider_sid` (e.g., ignore duplicates within 24h). [DP-4]
   - If this is the first “received/complete” notification, create `InboundFax` with status “received”.
   - Fetch the PDF from provider API over TLS using your configured credentials (new helper in `phaxio_service.py`). Store file to local/S3; compute SHA256 and size.
   - Assign mailbox via `InboundRule` lookup by `to_number`.
   - Generate `pdf_token` with TTL `inbound_token_ttl_minutes` and construct tokenized URL `PUBLIC_API_URL/inbound/{id}/pdf?token=...`.
   - Audit: `inbound_received` with `id` and `backend="phaxio"` (no PHI).
3. Respond 200 to provider.

Error handling:
- On invalid signature → 401
- On missing/invalid required fields → 400
- On fetch/store failure → record `error` and 500; provider will retry; ensure idempotency.


### 7) Sinch Inbound Flow (Cloud Backend)

Mirror the Phaxio approach with Sinch Fax v3:

1. Configure inbound callbacks to `/sinch-inbound`.
2. Verify provider auth/signature per configured method (env-driven; implement strict mode by default).
3. Parse fax ID, from_number, to_number, status. Enforce idempotency. [DP-4]
4. Fetch PDF via Sinch API; store; compute SHA256 and page count if available.
5. Assign mailbox; generate token; audit `inbound_received` with `backend="sinch"`.
6. Respond 200.


### 8) SIP/Asterisk Inbound Flow (Self-Hosted)

Prereqs:
- SIP trunk supports T.38. NAT/UDPTL ports open (4000–4999 by default). Keep AMI (5038/tcp) internal only.

Dialplan snippet (example — keep backend-specific):

```
[fax-inbound]
exten => _X.,1,NoOp(Fax inbound DID ${EXTEN} from ${CALLERID(num)})
 same => n,Set(FAXFILE=/var/spool/asterisk/fax/${UNIQUEID}.tiff)
 same => n,ReceiveFAX(${FAXFILE})
 same => n,Set(FAXSTATUS=${FAXSTATUS})
 same => n,Set(FAXPAGES=${FAXPAGES})
 same => n,Set(TO_DID=${EXTEN})
 same => n,Set(FROM_NUM=${CALLERID(num)})
 ; Notify Faxbot internal endpoint (use curl or AGI)
 same => n,System(curl -s -X POST http://faxbot-api:8080/_internal/asterisk/inbound \
   -H "X-Internal-Secret: ${ASTERISK_INBOUND_SECRET}" \
   -H "Content-Type: application/json" \
   -d '{"tiff_path":"${FAXFILE}","to_number":"${TO_DID}","from_number":"${FROM_NUM}","faxstatus":"${FAXSTATUS}","faxpages":"${FAXPAGES}","uniqueid":"${UNIQUEID}"}')
 same => n,Hangup()
```

Internal endpoint handler `POST /_internal/asterisk/inbound`:
- Require header `X-Internal-Secret` equals `settings.asterisk_inbound_secret`.
- Validate file path is under expected directory; refuse traversal.
- Convert TIFF→PDF using existing Ghostscript path; compute SHA256 and size.
- Create `InboundFax` with backend `"sip"`, set `provider_sid` to `uniqueid`, set pages from `faxpages` if present.
- Assign mailbox by `to_number` rule; set token; audit `inbound_received` with `backend="sip"`.
- Return 200.

[DP-8] Choose AMI UserEvent vs AGI/curl: AGI/curl is simpler; AMI UserEvent requires a persistent listener (we already have AMI client). Either is acceptable; start with AGI/curl for simplicity.

[DP-9] Keep `X-Internal-Secret` rotation documented; never expose this endpoint publicly. Use private DNS or Docker network.


### 9) Tokenized Retrieval

- `GET /inbound/{id}/pdf?token=...` returns the PDF if the token matches and is not expired. Use the same pattern as outbound Phaxio file serving: equality check + expiry against `pdf_token_expires_at`. Return 403 on mismatch/expired; 404 if missing file.
- Alternative authenticated path: allow `X-API-Key` with `inbound:read` to fetch without token (optional, configurable).
- Set headers: `Cache-Control: no-cache, no-store, must-revalidate` plus `Pragma`/`Expires`.

[DP-2] Choose default token TTL. For user downloads, 60 minutes is common; provider fetch tokens may be shorter (e.g., 10–15 minutes).


### 10) Retention and Cleanup

- New background task mirrors existing artifact cleanup: periodically delete inbound PDFs/TIFFs older than `INBOUND_RETENTION_DAYS` (if >0) and mark access as unavailable.
- Compute `retention_until` at create time. Delete from storage; remove TIFF after conversion.
- Audit `inbound_deleted` with `id` (no PHI).

[DP-3] Decide retention days for PHI; typical healthcare policies: 7–30 days for transient fax artifacts if a separate EHR stores the permanent record. If Faxbot is the system of record, retention policies must reflect legal requirements (consult compliance).


### 11) Rate Limiting (Optional but Recommended)

- Apply per-key limits for metadata and download to mitigate scraping/abuse.
- Config: reuse `MAX_REQUESTS_PER_MINUTE` or add `INBOUND_MAX_REQUESTS_PER_MINUTE`. Start simple: fixed-window per key and route.
- On exceed, return 429 and `Retry-After`.

[DP-5] Pick initial per-minute limits (e.g., list: 30/min; get/pdf: 60/min).


### 12) Audit and Observability

- Events:
  - `inbound_received` (id, backend)
  - `inbound_pdf_served` (id, method=token|api_key)
  - `inbound_deleted` (id)
  - `inbound_error` (id, reason)
- No PHI in events. Use job or inbound IDs and backend labels only.
- Metrics (optional): total inbound, failures by backend, average fetch time, conversion errors, token validation failures.

[DP-11] Decide where to send metrics/logs (stdout vs syslog vs OTEL). Reuse existing audit config if possible.


### 13) SDK Additions (Node + Python)

Identical API surface for inbound:

- `list_inbound(query?: { to?: string; since?: string; status?: string; mailbox?: string })`
- `get_inbound(id: string)`
- `download_inbound_pdf(id: string)` or `get_inbound_pdf_url(id: string)`

Error mapping remains consistent: 400/401/403/404/415. All methods accept optional API key via env or constructor.

[DP-10] Decide whether SDKs should stream file data or return a tokenized URL (URL is simpler; streaming avoids exposing URLs in logs).


### 14) MCP Integration (Caution: PHI)

- Add metadata-only tools to avoid PHI exposure to assistants by default:
  - `list_inbound_faxes` (no PDFs)
  - `get_inbound_fax_status` (metadata only)
- If you must add a PDF-returning tool, gate it behind SSE+OAuth transport and strict policy. Never log contents. Consider not implementing PDF streaming at MCP layer for HIPAA users.


### 15) Testing Matrix

Common:
- Create mailbox and rule for a test DID (+15551234567 → “Referrals”).
- Verify list/get filters, scope checks, and rate limits.
- Verify tokenized download (valid token, expired token, invalid token).
- Verify retention cleanup removes files and blocks future access.

Phaxio (cloud):
- Simulate a signed inbound callback to `/phaxio-inbound` (use your secret to compute HMAC over body). Ensure idempotency on duplicate events.
- Mock provider PDF fetch (add test flag) or run against sandbox if available. Validate stored file, SHA256, pages if provided.

Sinch (cloud):
- Simulate provider callback validation. Similar to Phaxio path.

SIP/Asterisk (self-hosted):
- Generate a sample TIFF and POST to `/_internal/asterisk/inbound` with secret and metadata. Verify PDF conversion, storage, and retrieval.
- If you have a lab Asterisk: place a real inbound fax call and trace `ReceiveFAX` variables.

Test (dev):
- `POST /_internal/test/inbound` (create synthetic inbound from a local PDF path or small embedded PDF) for CI/dev.


### 16) Hardening and Scale

- Idempotency store: table keyed by provider SID and event type with timestamps. Ignore duplicates within a window (e.g., 24–48h). [DP-4]
- External workers: for heavy TIFF→PDF conversions, consider background queues (RQ/Celery). [DP-12]
- Storage encryption: at-rest encryption always; consider app-level encryption when storage trust boundaries require it. [DP-7]
- Access isolation: in multi-tenant scenarios, include tenant/org IDs in mailbox and inbound rows and enforce via scopes.


### 17) Rollout Plan

1. Implement SIP/Asterisk internal endpoint and synthetic test endpoint; verify conversion and retrieval locally.
2. Add Phaxio inbound, then Sinch inbound; test signature verification and fetch flows.
3. Add mailbox/rule admin endpoints; verify routing and RBAC.
4. Add retention cleanup and audit events; validate non-PHI logs.
5. Update SDKs and (optionally) MCP tools for metadata.
6. Stage with HTTPS and provider callbacks; validate end-to-end.


## API Specs (Proposed)

Note: Keep request/response shapes minimal. Expand only if necessary.

`GET /inbound`
- Auth: `X-API-Key` with `inbound:list`
- Query: `to_number?`, `status?`, `since?`, `mailbox?`
- 200: `[ { id, from, to, status, backend, pages, created_at, received_at, mailbox } ]`

`GET /inbound/{id}`
- Auth: `X-API-Key` with `inbound:read`
- 200: `{ id, from, to, status, backend, pages, size_bytes, sha256, created_at, received_at, mailbox }`
- 404 if not found

`GET /inbound/{id}/pdf?token=...`
- Auth: token OR API key (if implemented)
- 200: PDF stream; headers: `Cache-Control: no-cache, no-store, must-revalidate`
- 403 on invalid/expired token; 404 if not found

`POST /phaxio-inbound`
- Auth: HMAC header `X-Phaxio-Signature`
- Body: provider payload (form or JSON)
- 200 on acceptance; 401 on signature mismatch; 400 on invalid body

`POST /sinch-inbound`
- Auth: provider verification (Basic/HMAC) — configurable
- Body: provider payload (JSON)
- 200/401/400 accordingly

`POST /_internal/asterisk/inbound`
- Auth: `X-Internal-Secret`
- Body (JSON): `{ tiff_path, to_number, from_number?, faxstatus?, faxpages?, uniqueid }`
- 200 on success; 401 on secret mismatch; 400 on validation error

Admin:
`POST /admin/mailboxes` → create `{ label, allowed_scopes?, note? }`
`GET /admin/mailboxes` → list
`PUT /admin/mailboxes/{id}` → update
`POST /admin/inbound/rules` → `{ to_number, mailbox_label }`
`GET /admin/inbound/rules` → list


## Open Decision Prompts (copy/paste to answer quickly)
Resolved decisions (filled)
- [DP-1] Storage backend: S3 with SSE‑KMS (under BAA). Keep local for dev; allow S3‑compatible (MinIO) for on‑prem.
- [DP-2] Token TTLs: provider fetch N/A for inbound; user download token 60 minutes (configurable).
- [DP-3] Retention: 30 days default (configurable). No archive by default; delete after retention.
- [DP-4] Idempotency: 48‑hour dedupe window on (provider_sid, event_type) with a unique index; ignore duplicates.
- [DP-5] Rate limits: list 30/min; get/pdf 60/min per key; callbacks exempt.
- [DP-6] Routing: start with simple DID→mailbox map; no rules engine yet.
- [DP-7] App‑level encryption: no (rely on TLS + SSE‑KMS). Revisit if policy requires.
- [DP-8] Asterisk notify: AGI/curl to internal endpoint (/_internal/asterisk/inbound); simplest and reliable.
- [DP-9] Internal secret: header X‑Internal‑Secret, rotate quarterly, restrict to private network/Docker overlay; optionally IP‑allowlist.
- [DP-10] SDK behavior: return tokenized URL by default; add streaming later if needed.
- [DP-11] Logs/metrics: structured JSON to stdout; ship via your stack (e.g., OTEL/Fluent Bit). Use existing audit hooks; no PHI.
- [DP-12] Workers: FastAPI background tasks now; leave a switch to external queue (RQ/Celery) if TIFF→PDF load grows.
- [DP-1] Storage backend: local vs S3 (bucket, prefix, region, KMS key)?
- [DP-2] Token TTLs: provider fetch token = __ minutes; user download token = __ minutes.
- [DP-3] Retention: delete after __ days; archive to __ (S3/KMS?) before delete? yes/no.
- [DP-4] Idempotency: provider SID window = __ hours; store table name = __.
- [DP-5] Rate limits: list = __/min; get/pdf = __/min.
- [DP-6] Routing: start with DID→mailbox map; rules engine needed now? yes/no.
- [DP-7] App-level encryption: enable now? yes/no. If yes, key management = __.
- [DP-8] Asterisk notification method: AMI UserEvent vs AGI/curl? __.
- [DP-9] Internal secret header name = __; rotation cadence = __; network scope = __.
- [DP-10] SDK behavior: return tokenized URL vs stream? __.
- [DP-11] Logs/metrics sink: stdout/syslog/OTEL? __.
- [DP-12] Workers: FastAPI background vs external queue? __.


## Acceptance Checklist

- [ ] Phaxio inbound: signed callback verified; PDF fetched; record created; token works; audit emitted.
- [ ] Sinch inbound: auth verified; PDF fetched; record created; token works; audit emitted.
- [ ] SIP/Asterisk inbound: TIFF converted to PDF; record created; token works; audit emitted.
- [ ] Listing and metadata require proper scopes; 401/403 enforced.
- [ ] Retention cleanup deletes files and blocks access; audit emitted.
- [ ] No PHI or tokens in logs; only IDs and generic metadata.
- [ ] Rate limiting triggers 429 as configured.
- [ ] SDK inbound APIs mirror behavior; MCP metadata tools do not expose PDFs.


## Risk Notes and Mitigations

- Interop: Cloud providers handle carrier interop. For SIP, choose a trunk known for fax and enable T.38; test G.711 fallback only if necessary.
- Security: Enforce TLS; verify signatures; keep AMI/internal endpoints private; no PHI in logs.
- Scale: Add idempotency, background conversions, and storage offloading. Horizontal scale later with shared DB/object storage.
- Compliance: Execute BAAs with providers/infrastructure; document retention policy; restrict access via API key scopes.


## Final Guidance

- Keep it simple and secure. Add complexity only when required by policy or scale.
- Keep backend-specific code isolated and small. Do not cross-leak provider code.
- Document every DP choice you make in commits and follow-up docs. Use the DP tags above for easy searchability.


## Development Workflow and Repository Strategy (Must Read)

Q: From a development flow perspective, if I want to add a front end and fax receiving capability to Faxbot, but I also have an MVP for consumers who just need to send faxes now (current state), should I fork the repo and make a separate commercial app (faxbot.net) and leave this one alone?

A: Short answer
- Don’t create a long‑lived fork of the core.
- Keep this repo as the canonical open‑core (continue adding generic capabilities like fax receiving here).
- Create a separate private repo (e.g., faxbot.net) for the commercial app that depends on the core via released packages/Docker images or a git submodule.

Why this is the better path
- Avoids divergence/merge‑hell: Security fixes and backend updates land once in core and flow into the app.
- Clear open‑core boundary: Core = protocol/backends/API/MCP/SDK; App = UI, billing, tenancy, provisioning, analytics.
- Faster iteration: The app can ship UI/business features without destabilizing core.
- Compliance separation: PHI handling and audit controls stay consistent in core; plan/tenant/billing live in app.

What goes where
- Core (this repo)
  - REST API endpoints, validation, size/type limits, HMAC verification
  - All backends (Phaxio/Sinch/SIP) send + receive plumbing
  - Webhook handlers per backend (kept isolated by backend)
  - File processing, tokenized file access, audit hooks
  - MCP servers and SDKs
- Commercial app (faxbot.net repo)
  - Frontend/dashboard (auth, orgs/teams, multi‑tenant)
  - Billing/plan limits, quotas, rate limiting, retention windows
  - Number provisioning UX, per‑tenant settings
  - Inbox UI, tagging/routing, notifications
  - Advanced analytics, exports, support tooling

Branching approach
- Keep `main` production‑stable. Do all new work (including PHASE_2.md and PHASE_RECEIVE.md) on `development`.
- Prefer short‑lived feature branches off `development` (e.g., `feat/phase-2-auth`, `feat/phase-receive`) → PR into `development` → stabilize → merge/release to `main`.
- Mark phase docs as draft/WIP on `development`. If docs publish to a site, gate WIP sections until merged to `main`.
- Tag releases from `main` (e.g., v1.1.0) so MVP users can pin stable versions.

Note: Dev workflow tends to be the biggest challenge — keep the above process explicit in PRs and commit messages.
