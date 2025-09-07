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
- api/mcp_sse_server.js:38-56 - OAuth authentication required for all endpoints
- No fallback mode for development or non-PHI scenarios
- docs/MCP_INTEGRATION.md doesn't distinguish between PHI and non-PHI usage

**Proposed fix:** Make OAuth optional via `REQUIRE_MCP_OAUTH=false` flag for non-healthcare deployments, with clear documentation about when it's needed.  
**Owner:**  
**Target date:**

### Missing Dockerfile Security {#dockerfile-security}
**Severity:** High  
**Why it matters:** Production API container runs as root without security hardening, expanding attack surface if compromised. HIPAA requires minimum necessary access principles.

**Evidence:**  
- api/Dockerfile:1-26 - No USER directive, runs as root
- api/Dockerfile.mcp:14-17 - MCP container properly uses non-root user
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
