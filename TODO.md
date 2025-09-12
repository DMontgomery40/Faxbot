# Full Codebase Review — Findings & Recommendations (2025‑Q3)

This section is a comprehensive, line‑by‑line oriented review intended to surface correctness bugs, security gaps, missing connections, incomplete features, redundancy, and UX pain points. It prioritizes actionable fixes and suggests intuitive helpers that reduce complexity for operators and contributors.

Executive summary
- Core send pipeline is solid; input validation, file sizing, and backend switching exist. New multi‑key auth, scopes, and rate‑limit plumbing are in place.
- Inbound scaffolding is good progress (SIP internal + cloud endpoints). Storage adapter (local/S3) gives us a clean production path.
- Biggest risks: unauthenticated Node MCP HTTP server, Asterisk AMI exposure in compose, default HTTPS/auth posture too relaxed for prod, test suite not green due to legacy fixtures, and some config knobs not being enforced as intended.
- Developer UX is still heavy; scripts added help, but environment validation and “one‑button” flows should be the norm.

P0 (Critical) — must fix before broad adoption
1) Node MCP HTTP server lacks server-side auth — DONE
   - Implemented API key authentication on all `/mcp` routes in `node_mcp/src/servers/http.js`.
   - New env: `MCP_HTTP_API_KEY` (required) and `MCP_HTTP_CORS_ORIGIN` (optional; default '*').
   - Docs updated (docs/MCP_INTEGRATION.md) and .env.example updated.

2) Docker Compose exposes Asterisk AMI publicly — DONE
   - Removed `5038:5038` mapping from docker-compose.yml; AMI remains internal only.
   - SIP_SETUP.md already warns; check-env script reports AMI credentials and advises internal use.

3) Test suite red flags (legacy Phaxio tests fail to collect) — DONE
   - File: api/tests/test_phaxio_integration.py expected a `phaxio_env_vars` fixture that did not exist.
   - Action: Removed the legacy file; consolidated coverage in `api/tests/test_phaxio.py` (authoritative harness).
   - Result: `pytest` now green locally (21 passed).

4) Security defaults too permissive for production — DONE
   - Change: Default `ENFORCE_PUBLIC_HTTPS=true` and `PHAXIO_VERIFY_SIGNATURE=true` in `api/app/config.py`.
   - Docs: Updated `.env.example` to set `ENFORCE_PUBLIC_HTTPS=true`.
   - Tests: Adjusted `test_phaxio_callback_handling` to disable signature verification for that unit test.
   - Outcome: Secure-by-default posture in production while preserving dev ergonomics (localhost exempt from HTTPS enforcement).

P1 (High) — high‑impact correctness and UX
5) Inbound rate‑limit knobs not actually used in limiter — DONE
   - Implemented per‑route override: `_enforce_rate_limit(info, path, limit)` with buckets keyed by `key_id|path`.
   - `require_inbound_list` uses `INBOUND_LIST_RPM`; `require_inbound_read` and `/inbound/{id}/pdf` (API‑key path) use `INBOUND_GET_RPM`.

6) Memory pressure: reads whole uploads into RAM — DONE
   - Implemented chunked streaming to disk with a 64 KB buffer and incremental size enforcement.
   - Added magic sniff: accept PDF by `%PDF` header; TXT when UTF‑8 decodes. Otherwise return 415.
   - Avoids double buffering by copying `orig_path` → `pdf_path` for PDFs and converting TXT→PDF.

7) Content‑type trust on upload
   - Trusts `UploadFile.content_type` without magic sniff. Adversaries can spoof to bypass 415 handling.
   - Fix: Minimal magic sniff (PDF “%PDF”, TXT ASCII/UTF‑8) before accepting; still cap to 10 MB.

8) Public HTTP defaults everywhere
   - Many docs/samples hardcode http://localhost; acceptable for dev, but we should loudly flag non‑TLS in prod and show TLS examples first.
   - Fix: Add “prod” snippets with HTTPS first; keep http examples under a “local dev” banner.

9) Duplicate / stale migration strategy
   - db.py has ad‑hoc `_ensure_optional_columns`; Alembic is now added. Two strategies can diverge.
   - Fix: Migrate fully to Alembic; drop ad‑hoc DDL once we have a first release with migrations.

10) AMI reconnect semantics
   - AMI reconnect tasks are spawned in `_read_loop()` on error. If Asterisk repeatedly flaps, tasks can stack.
   - Fix: Gate reconnection to a single task (use a state flag) and ensure writer/reader cleanup.

P2 (Medium) — correctness, scaling, ergonomics
11) Rate limiter buckets unbounded — DONE
   - Implemented minute‑window pruning in `_enforce_rate_limit` to drop stale buckets and bound memory.

12) Inbound retention cleanup — DONE
   - Added inbound cleanup in the periodic loop: deletes stored PDFs (S3/local) when `retention_until` has passed; clears TIFF paths; emits audits.

13) Inbound storage duplicate artifacts — DONE
   - When `put_pdf` returns an S3 URI, the local PDF is removed in all inbound paths (SIP, Phaxio, Sinch).

14) Inbound fetch from cloud providers is basic
   - For Phaxio/Sinch inbound, fetching PDF from a URL may require authenticated fetch or a separate API call by fax ID.
   - Fix: Add provider helper methods to fetch by ID with credentials and retry logic.

15) Node MCP HTTP/SSE CORS
   - Both allow `origin: '*'`. Fine for tools, but risky if ports are exposed.
   - Fix: Restrict CORS to trusted origins or disable CORS by default; document risk.

16) Python MCP SSE token verification errors are opaque
   - On failure returns 401 without detail; okay, but logs could include kid mismatch reasons.
   - Fix: Add debug logs (guarded) and metrics for auth failures.

17) Python MCP (server.py) uses `asyncio` without import
   - File: python_mcp/server.py — `asyncio` referenced in `main()` but not imported.
   - Fix: `import asyncio` at top.

18) Default backend is `sip`
   - File: api/app/config.py defaults `FAX_BACKEND=sip`.
   - Risk: New users will trip into SIP without telephony; Phaxio is easiest.
   - Fix: Default to `phaxio` for UX; require explicit opt‑in to SIP.

19) Phone number normalization is permissive
   - Regex allows 6–20 digits; non‑E.164 works. Mapping to E.164 is partial in Phaxio service only.
   - Fix: Centralize number normalization + strict E.164 (configurable) to reduce edge cases.

20) get_fax_pdf logging
   - Logs access via `logger.info` and audit_event; OK, but add client source info (IP) when available and ensure no token is logged.

21) Scripts robustness
   - `load-env.sh` simple source may misparse exotic values or multiline secrets.
   - Fix: Consider dotenv parser or require export lines; good enough for now.

Developer UX — missing helpers that make life easier
22) Config introspection endpoint
   - Add `/admin/config` (sanitized) to render effective config: selected backend, key enforcement, inbound/storage flags. No secrets.

23) Health with dependencies
   - `/health` returns ok regardless of backend readiness. Add `/health/ready` that checks AMI connectivity (SIP), Phaxio/Sinch config presence, writeability of storage, DB connectivity.

24) Scope helper
   - Create `require_scopes([...])` dependency to avoid duplicating `require_fax_send/read/inbound:list/read` patterns.

25) Unified provider abstraction
   - A slim interface (send/status/cancel, inbound_fetch_by_id) would hide differences between Phaxio/Sinch and reduce repeated mapping code.

26) Redaction utilities
   - Small `redact()` helper to mask phone numbers, URLs, tokens in logs consistently.

27) End‑to‑end dev flows
   - Add `scripts/dev-up.sh` that runs API + Asterisk, checks env with `scripts/check-env.sh`, then runs `smoke-auth.sh` and `inbound-internal-smoke.sh` automatically.

28) Example dialplans as templates
   - Provide ready‑to‑use `extensions.conf` snippets (send and inbound contexts) parameterized by env.

Documentation — targeted fixes
29) Consolidate security defaults
   - Have a “Production Profile” doc with exact env diffs (REQUIRE_API_KEY, ENFORCE_PUBLIC_HTTPS, PHAXIO_INBOUND_VERIFY_SIGNATURE, S3_KMS, Postgres DATABASE_URL) and one `scripts/check-env.sh` run.

30) MCP security matrix
   - Table listing stdio/HTTP/SSE transports, their auth modes, and when to use them. Explicit warning for MCP HTTP without server auth (P0 above).

31) Asterisk 101
   - A short primer (we added some) + links to trustworthy guides; document that AMI has no web GUI by design.

Known correctness bugs (quick wins)
- python_mcp/server.py: missing `import asyncio` (P1/17).
- Inbound per‑route limits not enforced (P1/5).
- Legacy tests require non‑existent fixtures (P0/3).

Backlog (ordered)
P0
- Add auth to Node MCP HTTP server; narrow CORS. [node_mcp/src/servers/http.js]
- Remove public AMI port mapping from docker-compose; add network isolation doc. [docker-compose.yml]
- Reconcile/remove failing legacy Phaxio tests; keep a single green harness. [api/tests]

P1
- Stream uploads to disk; add magic sniff for PDF/TXT. [api/app/main.py]
- Implement per‑route limits (inbound list/get) with `_enforce_rate_limit(info, limit, path)`. [api/app/main.py] — DONE
- Add inbound retention cleanup + S3 delete. [api/app/main.py, api/app/storage.py] — DONE
- Fix python_mcp/server.py to import `asyncio`. — DONE
- Default backend to `phaxio` for UX; require explicit `sip` for telephony users. [api/app/config.py] — DONE

P2
- Add `/admin/config` and `/health/ready`. [api/app/main.py] — DONE
- Consolidate provider abstraction with `inbound_fetch_by_id`. [api/app/phaxio_service.py, sinch_service.py]
- Add `require_scopes([...])` to DRY dependencies. [api/app/main.py]
- Add redaction helper for logs. [api/app/audit.py]
- Add `scripts/dev-up.sh` to orchestrate dev flow.

Notes on current strengths
- Clear separation of backends and transport layers; REST API remains the single integration point.
- Scopes + multi‑key auth significantly improve operational safety.
- Storage abstraction (local/S3) and inbound tokenized access are the right primitives for HIPAA contexts.


# Faxbot Security Audit Report - Critical & High Priority Findings

# Faxbot Security Audit Report - Critical & High Priority Findings

## Immediate
- Implement Sinch webhook handling to reach parity with Phaxio
  - Add `/sinch-callback` endpoint and signature/auth validation per Sinch docs
  - Map provider events to queued/in_progress/SUCCESS/FAILED
  - Update tests and docs (API_REFERENCE.md, SINCH_SETUP.md, TROUBLESHOOTING.md)
  - Consider configurable verification and retention rules analogous to Phaxio HMAC

## Executive Summary

**Critical security and compliance issues identified:**

• **[CRITICAL - Phaxio HMAC Disabled by Default](#phaxio-hmac-disabled)** - Webhook signature verification disabled, allowing unauthenticated callbacks
• **[CRITICAL - AMI Network Exposure](#ami-network-exposure)** - Asterisk Manager Interface exposed on public ports without warnings
• **[HIGH - API Key Optional](#api-key-optional)** - Authentication completely disabled when API_KEY is blank
• **[HIGH - HTTP Enforcement Gaps](#http-enforcement-gaps)** - HTTPS enforcement disabled by default, allowing PHI over HTTP
• **[HIGH - PHI in Logs](#phi-logging-risk)** - Request bodies and PDF content potentially logged in error cases  
• **[HIGH - Overbearing OAuth Requirements](#overbearing-oauth)** - MCP OAuth2 required for all scenarios including local dev
• **[HIGH - Missing Dockerfile Security](#dockerfile-security)** - Production Dockerfile runs as root with no security hardening
• **[HIGH - Weak Default Passwords](#weak-credentials)** - Example configuration uses "changeme" as default AMI password

---

## Critical Issues

### Phaxio HMAC Disabled by Default {#phaxio-hmac-disabled}
**Severity:** Critical  
**Why it matters:** Webhook callbacks can be spoofed by attackers to manipulate fax status, potentially hiding transmission failures or injecting false delivery confirmations. This violates HIPAA integrity controls (164.312(c)(1)).

**Evidence:**  
- api/app/config.py:29 - `phaxio_verify_signature: bool = Field(default_factory=lambda: os.getenv("PHAXIO_VERIFY_SIGNATURE", "false").lower() in {"1", "true", "yes"})`
- .env.example:21 - `PHAXIO_VERIFY_SIGNATURE=true` (but defaults to false in code)
- api/app/main.py:325-334 - HMAC verification only runs if `settings.phaxio_verify_signature` is true

**Proposed fix:** Change default to true in config.py and require explicit opt-out for non-production environments. Add startup warning when disabled.  
**Owner:**  
**Target date:**

### AMI Network Exposure {#ami-network-exposure}
**Severity:** Critical  
**Why it matters:** Asterisk Manager Interface (AMI) on port 5038 allows full control of telephony infrastructure. Public exposure enables unauthorized call origination and system compromise, violating HIPAA access control requirements (164.312(a)).

**Evidence:**  
- docker-compose.yml:33 - `"5038:5038" # AMI` port mapped without network restrictions
- docs/SIP_SETUP.md mentions keeping AMI "internal" but docker-compose exposes it publicly
- api/app/config.py:17-19 - AMI credentials configurable but no network isolation enforced

**Proposed fix:** Remove AMI port from docker-compose.yml public mapping, add network restrictions, and implement IP allowlists for AMI access.  
**Owner:**  
**Target date:**

---

## High Priority Issues

### API Key Optional {#api-key-optional}
**Severity:** High  
**Why it matters:** When API_KEY is blank, all fax endpoints are completely unauthenticated, allowing anyone to send faxes and access job status. This violates HIPAA access control and audit requirements.

**Evidence:**  
- api/app/main.py:102-104 - `require_api_key` function allows access when `settings.api_key` is falsy
- api/app/main.py:44-45 - Startup warning but doesn't prevent operation
- .env.example:6 - `API_KEY=your_secure_api_key_here` (placeholder)

**Proposed fix:** Require API key in production mode or add explicit `DISABLE_AUTH=true` flag with stronger warnings.  
**Owner:**  
**Target date:**

### HTTP Enforcement Gaps {#http-enforcement-gaps}
**Severity:** High  
**Why it matters:** PHI-containing PDFs can be transmitted over unencrypted HTTP to cloud providers. HIPAA requires encryption in transit (164.312(e)(1)).

**Evidence:**  
- api/app/config.py:43 - `enforce_public_https` defaults to false
- api/app/main.py:48-54 - Only warns about HTTP in non-localhost scenarios
- Only enforced when `fax_backend == "phaxio"`, not for other cloud backends

**Proposed fix:** Default `enforce_public_https` to true and require explicit opt-out for development environments.  
**Owner:**  
**Target date:**

### PHI in Logs {#phi-logging-risk}
**Severity:** High  
**Why it matters:** Error handling may log request bodies or PDF content, potentially exposing PHI in logs. HIPAA prohibits PHI in logs (164.312(d)).

**Evidence:**  
- api/app/main.py:302-306 - Logs PDF access events with job ID
- MCP server error handling may log file content in base64 form
- No explicit PHI redaction in audit.py for error scenarios

**Proposed fix:** Implement comprehensive PHI redaction in all logging paths and audit.py, especially for error cases.  
**Owner:**  
**Target date:**

### Overbearing OAuth Requirements {#overbearing-oauth}
**Severity:** High  
**Why it matters:** OAuth2 is mandatory for all MCP SSE connections, including local development, creating friction for non-healthcare users who don't need HIPAA-level security.

**Evidence:**  
- Node/Python SSE servers enforce OAuth2 (expected for HIPAA). Non-PHI users should prefer stdio/HTTP with API keys.

**Proposed fix:** Make OAuth optional via `REQUIRE_MCP_OAUTH=false` flag for non-healthcare deployments, with clear documentation about when it's needed.  
**Owner:**  
**Target date:**

### Missing Dockerfile Security {#dockerfile-security}
**Severity:** High  
**Why it matters:** Production API container runs as root without security hardening, expanding attack surface if compromised. HIPAA requires minimum necessary access principles.

**Evidence:**  
- api/Dockerfile:1-26 - No USER directive, runs as root
- asterisk/Dockerfile:1-28 - Also runs as root

**Proposed fix:** Add non-root user to production Dockerfile, implement security hardening options, and restrict container capabilities.  
**Owner:**  
**Target date:**

### Weak Default Passwords {#weak-credentials}
**Severity:** High  
**Why it matters:** Example configuration contains "changeme" password for AMI, likely to be used in production deployments without being changed.

**Evidence:**  
- .env.example:30 - `ASTERISK_AMI_PASSWORD=changeme`
- Comment warns to change it but still provides weak default

**Proposed fix:** Remove default password, require explicit configuration, and add validation to reject common weak passwords.  
**Owner:**  
**Target date:**

---

## Deferred/Non-Blocking (FYI)

**Medium Priority Items:**
- SDK version alignment between Node.js and Python (both at 1.0.2 but should verify release synchronization)
- Missing comprehensive input validation on phone number formats (regex allows very broad patterns)
- Docker Compose Asterisk service assumes local SIP trunk setup without validation
- Cleanup task in api/app/main.py:232-262 uses naive iteration that won't scale with large job databases
- MCP servers log sensitive information (phone numbers, job IDs) to console in development mode

**Documentation Gaps:**
- HIPAA_REQUIREMENTS.md mentions controls not yet implemented in codebase
- Missing BAA templates and risk analysis templates referenced in documentation
- Troubleshooting guide doesn't cover security-specific error scenarios
