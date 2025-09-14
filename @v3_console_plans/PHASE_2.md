# Faxbot Phase 2 — API Keys and Access Control (Agent Implementation Guide)

Status: WIP/Draft — Lives on development branch. Do not publish or rely on as final until merged to main. Sections and interfaces may change.

This document is a hands-on, step-by-step prompt for an implementation agent (or a junior developer) to deliver Phase 2 of Faxbot. In this project naming, “Phase 2” is the next major stage of the product, and it contains three implementation sub‑phases you will complete in order:

- Phase 1: Multi-key authentication (DB-backed API keys)
- Phase 2: Scopes, audit, rotation, and basic rate limiting
- Phase 3: Optional identity integration (mapping keys to users or JWT/OIDC alignment)

Read carefully and follow every step. Do not improvise patterns; use the existing codebase conventions and keep changes small and targeted. When something is ambiguous, choose the simplest path that works and is secure by default.


## Ground Rules (Must Read)

- Project name is “Faxbot”. The MCP server name is “faxbot-mcp”. Do not rename anything.
- Backends are mutually exclusive. Do not mix Phaxio and SIP/Asterisk instructions in the same code paths or docs.
- Supported file types are PDF and TXT only. There is no OCR in the pipeline.
- HIPAA users require strict defaults; non-HIPAA users get relaxed dev defaults. Your code must support both by configuration.
- Existing REST API endpoints must preserve their behavior, inputs, and outputs. We are only adding authentication capabilities and admin endpoints.
- Do not log PHI or credentials. Never log API key plaintext or tokens; you may log a non-sensitive key identifier.
- Use consistent error codes: 400/401/404/413/415 as already enforced in the API.
- Keep changes minimal and focused. Follow the current tech stack: FastAPI, SQLAlchemy, SQLite by default.


## Outcomes and Success Criteria

By the end, the API will support multiple API keys with hashed storage, scopes, auditing, and optional rate limits — without breaking existing clients. Specifically:

- Existing `API_KEY` env still works (shared “bootstrap” key), but DB-backed keys can be issued and used concurrently.
- The `X-API-Key` header is required in production when configured. For dev, it can still be optional.
- Admin endpoints exist to create, list, revoke, and rotate API keys. Plaintext is shown only once at creation/rotation.
- Keys can have optional scopes and expiration; revoked/expired keys are rejected with 401/403.
- Audit events record key issuance, revocation, and usage by key ID (no plaintext secret).
- MCP integrations keep working as-is; they just pass the key they are configured with.


## Repo Orientation (What’s already there)

- API service: `api/app/main.py` (FastAPI)
- Config: `api/app/config.py` (Pydantic Settings)
- DB models and init: `api/app/db.py` (SQLAlchemy, SQLite by default)
- Audit logging: `api/app/audit.py` (already used by job events)
- Backends: Phaxio in `api/app/phaxio_service.py`, Sinch in `api/app/sinch_service.py`, SIP via Asterisk AMI in `api/app/ami.py`
- MCP (Node/Python): calls the REST API, already supports `X-API-Key` header if present


## High-Level Plan (Three Sub‑Phases)

1) Phase 1 — Multi-Key Auth (DB-backed)
   - Add an `api_keys` table, hashed secrets, issuance endpoint, verification path in a new `auth.py` module.
   - Continue to accept the existing env `API_KEY` for bootstrapping/compatibility.
   - Introduce `REQUIRE_API_KEY` env to force auth in all environments (HIPAA default true in prod).

2) Phase 2 — Scopes, Audit, Rotation, Rate Limits
   - Add scopes like `fax:send`, `fax:read`, `keys:manage`.
   - Record `created_at`, `last_used_at`, `revoked_at`, `expires_at`.
   - Add endpoints for list/revoke/rotate and basic per-key rate limiting (fixed window or token bucket).

3) Phase 3 — Optional Identity Integration
   - Add optional mapping to a simple `users` table or align to existing OAuth/JWT used by MCP SSE.
   - Keys may reference `user_id` or `owner` label; no complex IAM needed.


## Detailed Implementation Steps (Do These In Order)

### 0. Prep and Guardrails

- Work on branch `development` (already created). Keep commits small and descriptive.
- Do not break existing endpoints or change response models.
- Keep admin endpoints separate, namespaced under `/admin/api-keys`.
- Reuse the existing audit logger via `audit_event()`; never print secrets.


### 1. Config Additions (config.py)

Add two new settings in `api/app/config.py`:

- `require_api_key: bool` from env `REQUIRE_API_KEY` (default false for dev).
- `max_requests_per_minute: int` from env `MAX_REQUESTS_PER_MINUTE` (default 0 = disabled). This applies per key for Phase 2.

Example default factories:

```python
require_api_key: bool = Field(default_factory=lambda: os.getenv("REQUIRE_API_KEY", "false").lower() in {"1", "true", "yes"})
max_requests_per_minute: int = Field(default_factory=lambda: int(os.getenv("MAX_REQUESTS_PER_MINUTE", "0")))
```


### 2. Database: New `api_keys` Table (db.py)

Create a new SQLAlchemy model in `api/app/db.py`:

```python
class APIKey(Base):  # type: ignore
    __tablename__ = "api_keys"
    id = Column(String(40), primary_key=True, index=True)          # UUID hex
    key_id = Column(String(32), unique=True, index=True, nullable=False)  # short identifier
    key_hash = Column(String(200), nullable=False)                 # stores algo$salt$b64hash
    name = Column(String(100), nullable=True)                      # display label
    owner = Column(String(100), nullable=True)                     # email/team/service label
    scopes = Column(String(200), nullable=True)                    # CSV or JSON (keep simple: CSV)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    last_used_at = Column(DateTime, nullable=True)
    expires_at = Column(DateTime, nullable=True)
    revoked_at = Column(DateTime, nullable=True)
    note = Column(Text, nullable=True)
```

Initialization:
- In `init_db()`, after `Base.metadata.create_all(engine)`, ensure `api_keys` exists. For SQLite, the normal create_all is sufficient; no destructive migrations needed.

Important: Do not store plaintext API keys. Only store salted hash material.


### 3. Auth Helpers (new file: api/app/auth.py)

Create a new module to encapsulate key generation and verification. Use only Python stdlib (no new dependencies):

- `generate_token() -> (token_str, key_id, secret)`
  - Generate `key_id` as 10–12 char URL-safe string (e.g., base32/hex of random 5–6 bytes).
  - Generate `secret` as 32–48 bytes (use `secrets.token_urlsafe(32)`)
  - Token format to return and show once to the user: `fbk_live_<key_id>_<secret>`

- `hash_secret(secret: str) -> str`
  - Use `hashlib.scrypt` (or `hashlib.pbkdf2_hmac` if scrypt not available) with random 16–32 byte salt.
  - Return string like: `scrypt$<salt_b64url>$<hash_b64url>$n=16384$r=8$p=1`

- `verify_secret(secret: str, key_hash: str) -> bool`
  - Parse the stored hash, recompute, and `hmac.compare_digest`.

- `parse_header_token(x_api_key: str) -> (key_id, secret)`
  - Validate prefix `fbk_`, split on `_`, ensure both parts are present.
  - If it doesn’t match our format, return `(None, None)` so env `API_KEY` path can still match.

Testing Tips:
- Write small unit tests for hash roundtrip and parsing (optional but recommended if tests already exist in repo).


### 4. Admin Endpoints (main.py)

Add a small set of admin endpoints under `/admin/api-keys`. Protect them with either the bootstrap env `API_KEY` or an API key that has the `keys:manage` scope.

New FastAPI dependency:

```python
def require_admin(x_api_key: Optional[str] = Header(default=None)):
    # Accept env API_KEY as admin always (bootstrap)
    if settings.api_key and x_api_key == settings.api_key:
        return {"admin": True, "key_id": "env"}
    # Else check DB key and require scope `keys:manage`
    info = verify_db_key(x_api_key)  # you will implement in auth helpers
    if not info or ("keys:manage" not in info.get("scopes", [])):
        raise HTTPException(401, detail="Admin authentication failed")
    return info
```

Endpoints to add:

1) `POST /admin/api-keys` — create a key
   - Request: `{ "name": "string", "owner": "string", "scopes": ["fax:send", "fax:read"], "expires_at": "ISO8601?" , "note": "optional" }`
   - Behavior: generate token, hash and store, return `{ keyId, token, name, owner, scopes, expires_at }` where `token` is shown ONCE.
   - Audit: `audit_event("api_key_created", key_id=keyId, owner=owner, scopes=scopes)`

2) `GET /admin/api-keys` — list metadata only
   - Response: array of `{ keyId, name, owner, scopes, created_at, last_used_at, expires_at, revoked_at, note }` (no hashes, no tokens).

3) `DELETE /admin/api-keys/{keyId}` — revoke
   - Behavior: set `revoked_at = now()`.
   - Response: `{ status: "ok" }`
   - Audit: `audit_event("api_key_revoked", key_id=keyId)`

4) `POST /admin/api-keys/{keyId}/rotate` — rotate/replace secret
   - Behavior: generate new secret (new token), update `key_hash`, return new `token` once.
   - Audit: `audit_event("api_key_rotated", key_id=keyId)`

Note: Use 401 on missing/invalid admin auth. Return 404 if keyId not found. Validate request bodies.


### 5. Request Authentication for Core Endpoints (main.py)

Replace the simple `require_api_key` dependency with DB-aware logic while preserving compatibility:

Rules:
- If `settings.require_api_key` is true, `X-API-Key` must be present and valid.
- If `settings.api_key` is set and matches header → allow (bootstrap/shared key).
- Else try DB-backed key:
  - Parse header token into `key_id` and `secret`.
  - Look up `APIKey` by `key_id`, ensure not revoked/expired.
  - Verify secret (scrypt); on success, update `last_used_at`.
  - Enforce scopes per endpoint: `/fax` requires `fax:send`; `/fax/{id}` requires `fax:read`.
- If `settings.require_api_key` is false and header is missing, allow requests (dev mode), but still accept and verify keys if provided.

Return 401 on authentication failure. Return 403 when token is valid but missing required scopes.


### 6. Basic Per-Key Rate Limiting (Phase 2)

Goal: optional fixed-window or token-bucket in-memory limiter by `key_id`.

- Config-driven: use `max_requests_per_minute`. If 0 → disabled.
- Store a simple in-memory map `{ key_id: { window_start_ts, count } }` (since API is single-process by default). For multi-instance deployments, this can be revisited.
- Apply to authenticated keys only. The env key may share the same bucket with `key_id = "env"`.
- On exceed → 429 Too Many Requests with a short `Retry-After` header (e.g., seconds remaining in the window).

Keep it simple; do not add external dependencies.


### 7. Audit Hooks

Use `audit_event()` for:
- `api_key_created`, `api_key_revoked`, `api_key_rotated`
- `api_key_used` (include `key_id` and endpoint slug, e.g., `"/fax"`) — ensure this does not log phone numbers or any PHI.

Honor `AUDIT_LOG_ENABLED` and existing logging configuration.


### 8. SDK and MCP Compatibility

- No SDK changes are required. They already send `X-API-Key` if configured.
- Document that developers should now create per-user/service tokens and supply them via env to SDKs and MCP servers:
  - Node MCP: set `API_KEY` env for `node_mcp` container/process.
  - Python MCP: set `API_KEY` env similarly.
  - REST clients: include `X-API-Key: <token>`.


### 9. Documentation Snippets (to add after implementation)

Provide examples for HIPAA vs non-HIPAA setups:

- HIPAA (strict):

```env
API_KEY=bootstrap_admin_only
REQUIRE_API_KEY=true
AUDIT_LOG_ENABLED=true
ENFORCE_PUBLIC_HTTPS=true
PHAXIO_VERIFY_SIGNATURE=true
```

- Non-HIPAA dev (relaxed):

```env
API_KEY=                               # optional
REQUIRE_API_KEY=false                  # dev convenience
AUDIT_LOG_ENABLED=false
ENFORCE_PUBLIC_HTTPS=false
```

CLI examples (after admin endpoints exist):

```bash
# Create a send/read key (admin auth via bootstrap env API_KEY)
curl -s -X POST http://localhost:8080/admin/api-keys \
  -H 'X-API-Key: bootstrap_admin_only' \
  -H 'Content-Type: application/json' \
  -d '{"name":"dev key","owner":"alice@example.com","scopes":["fax:send","fax:read"]}'

# Use key to send a fax
curl -s -X POST http://localhost:8080/fax \
  -H 'X-API-Key: fbk_live_<keyId>_<secret>' \
  -F to=+15551234567 \
  -F file=@/path/to/doc.pdf
```


## Acceptance Tests (What to Manually Verify)

1) Existing behavior works:
   - With `API_KEY` set and no DB keys, `/fax` and `/fax/{id}` work using that key.
   - With `API_KEY` unset and `REQUIRE_API_KEY=false`, endpoints accept unauthenticated calls (dev mode).

2) DB-backed keys:
   - Create a key via `/admin/api-keys`, receive a single-use plaintext token.
   - Use that token to call `/fax` (send) and `/fax/{id}` (read) according to scopes.
   - Revoking the key blocks subsequent use with 401/403.

3) Scopes:
   - A key with only `fax:read` fails on `POST /fax` with 403.
   - A key with only `fax:send` fails on `GET /fax/{id}` with 403.

4) Expiration:
   - Create a key with `expires_at` in the past; verify 401/403.

5) Rate limiting (if enabled):
   - Set `MAX_REQUESTS_PER_MINUTE=3`, call `/fax` or `/fax/{id}` >3 times within a minute; get 429.

6) Audit:
   - With `AUDIT_LOG_ENABLED=true`, confirm audit entries for create/revoke/use (only keyId, no plaintext secrets).


## Common Pitfalls (Avoid These)

- Do not store plaintext keys or secrets in the DB; store only salted hashes.
- Do not log tokens, secrets, phone numbers, or PDF contents in any logs or audits.
- Do not break the public REST API schema or error codes.
- Do not block Phaxio callbacks or change `/fax/{job_id}/pdf` token logic — this is separate from API keys.
- Keep backend separation: Asterisk/SIP details must not leak into Phaxio/Sinch paths.


## Stretch Goals (Optional, Only If Time Allows)

- Add ETag/If-Modified-Since headers to reduce load (non-PHI endpoints only).
- Add a simple in-memory cache for key lookups with a short TTL (e.g., 30s) to reduce DB hits.
- Add a CLI helper script under `scripts/` to call the admin endpoints.


## Rollout Plan

1) Implement Phase 1 (DB keys) behind `REQUIRE_API_KEY` flag; test locally.
2) Add Phase 2 features (scopes/audit/rate-limit) and enable in staging.
3) Update docs with clear HIPAA vs non-HIPAA guidance (no backend mixing).
4) Optionally prototype Phase 3 identity mapping for future UI/front-end needs.


## Done Checklist (Mark All Before Hand-off)

- [ ] `api_keys` model and migrations work on fresh SQLite.
- [ ] `auth.py` helpers (generate, hash, verify, parse) with tests or manual verification.
- [ ] Admin endpoints: create, list, revoke, rotate.
- [x] Request dependency enforces keys correctly and preserves env `API_KEY` compatibility.
- [x] Scopes enforced on `/fax` (send) and `/fax/{id}` (read).
- [x] Optional rate limiting returns 429 when exceeded.
- [ ] Audit events fire with key IDs only.
- [ ] Docs snippets prepared for HIPAA and non-HIPAA.


## Final Notes for the Implementer

- Keep PRs small and focused. Include brief descriptions and testing notes.
- If you must choose between complexity and security, choose security with simple code.
- Ask for clarification if you’re unsure — do not guess about HIPAA or backend behavior.


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
  - File processing (TXT→PDF, TIFF handling for SIP), tokenized file access, audit hooks
  - MCP servers (Node/Python, stdio/HTTP/SSE) and SDKs (Node/Python)
- Commercial app (faxbot.net repo)
  - Frontend/dashboard (auth, orgs/teams, multi‑tenant)
  - Billing/plan limits, quotas, rate limiting, retention windows
  - Number provisioning UX (Phaxio/Sinch flows), per‑tenant settings
  - Inbox UI for receiving, search, tagging, routing rules, notifications
  - Advanced analytics, exports, support tooling

Receiving capability recommendation
- Implement inbound fax support in core (benefits all users and keeps backend logic centralized):
  - Phaxio/Sinch: Add inbound webhook endpoint with HMAC/signature verification; create records and tokenized file access.
  - SIP/Asterisk: Add inbound dialplan/AMI/AGI handler to receive T.38, convert to PDF, store artifact, and expose list/detail endpoints.
- Keep backend docs strictly separated per AGENTS.md (no mixed instructions).

Branching approach
- Keep `main` production‑stable. Do all new work (including PHASE_2.md and PHASE_RECEIVE.md) on `development`.
- Prefer short‑lived feature branches off `development` (e.g., `feat/phase-2-auth`, `feat/phase-receive`) → PR into `development` → stabilize → merge/release to `main`.
- Mark these phase docs as draft/WIP while on `development`. If docs are published, gate WIP sections from the public site until merged to `main`.
- Tag releases from `main` (e.g., v1.1.0) so MVP consumers can pin stable versions.

Note: Dev workflow is a common pain point — keep the above process explicit in PRs and commit messages.
