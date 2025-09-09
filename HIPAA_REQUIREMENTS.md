# HIPAA_REQUIREMENTS.md

This document describes what is required to operate Faxbot in a HIPAA‑aligned manner. It is a technical guide and checklist for engineers and operators. It is not legal advice. Always consult your compliance team and counsel. You (the operator) are responsible for implementing and documenting the controls below and for executing a formal risk analysis and governance program.

## Scope & Data Flows
- Covered workflows: sending faxes that may contain PHI.
- Not covered: receiving faxes (non‑goal), messaging, IVR, EHR integrations.

Backends (choose one):
- Phaxio (cloud): Client → Faxbot API → Phaxio → PSTN/Fax. Phaxio fetches the PDF from your `PUBLIC_API_URL` and posts status callbacks.
- SIP/Asterisk (self‑hosted): Client → Faxbot API → Asterisk (T.38/UDPTL) → SIP trunk → PSTN/Fax.

PHI touchpoints:
- PDF/TXT upload to Faxbot API.
- Stored job artifacts (original, PDF, TIFF for SIP).
- Status updates (Phaxio callbacks or Asterisk AMI user events).
- Application/Reverse proxy logs (must be PHI‑free).

## Roles & Agreements
- If you are a Covered Entity or Business Associate, you must:
  - Execute a BAA with any cloud provider that may handle PHI (e.g., Phaxio). Contact provider sales to obtain a BAA; do not use without a BAA.
  - Treat Faxbot and Asterisk operators as Business Associates if they are separate entities.
- Self‑hosted SIP stack does not remove HIPAA obligations; it moves them to you.

## Technical Safeguards (Security Rule)
Implement the following as minimum controls:

1) Transport security
- Public API must be served over HTTPS. Use TLS certs from a reputable CA.
- For Phaxio backend:
  - `PUBLIC_API_URL` must be HTTPS in production.
  - Enable callback signature verification (default on): `PHAXIO_VERIFY_SIGNATURE=true`. Server verifies `X-Phaxio-Signature` (HMAC‑SHA256 over raw body with `PHAXIO_API_SECRET`).
- For SIP backend:
  - SIP signaling should use TLS if supported by your provider; media (T.38 over UDPTL) is typically not encrypted. Mitigate with a site‑to‑site VPN/private interconnect to your SIP provider and strict firewalling.
  - Never expose AMI (5038/tcp) to the public internet.

-2) Access control
- Require API key on all /fax and /fax/{id} calls (`X-API-Key`). Set `REQUIRE_API_KEY=true` to enforce authentication even if env `API_KEY` is blank. Use DB‑backed, per‑user/service API keys created via admin endpoints instead of sharing a single env key. Rotate keys regularly.
- Restrict inbound traffic with a reverse proxy: IP allowlists and rate limiting.
- Optional app-level rate limiting: set `MAX_REQUESTS_PER_MINUTE` to bound per-key request rates in addition to reverse proxy/WAF limits.
- Rotate credentials and set a strong AMI password. Do not use `changeme`.

3) Data minimization & confidentiality
- Do not log PHI. Ensure request bodies (PDF/TXT) and rendered content are never logged.
- Faxbot redacts tokenized PDF URLs from logs.
- Tokenized PDF access:
  - The server issues a per‑job, random `pdf_token` with a short TTL (`PDF_TOKEN_TTL_MINUTES`, default 60). The `/fax/{job_id}/pdf` endpoint requires exact token equality and enforces expiry.
  - Keep TTL as short as operationally feasible.

4) Storage security (at rest)
- Store database and artifacts on encrypted volumes or use a managed, encrypted database. SQLite is acceptable only if disk encryption and backups are in place.
- Separate storage for development vs production. Limit admin access and use MFA on hosts.
- Data retention policy: delete PDFs/TIFFs after transmission completes and your minimum retention requirement is satisfied.

5) Integrity & auditing
- Maintain audit logs of access to `/fax/{job_id}/pdf`, job creation, and status changes. No PHI in logs; use job IDs and metadata only.
- Time synchronize servers (NTP) for accurate audit trails.

6) Availability & recovery
- Back up database (and optionally artifacts) on a secure, encrypted target with rotation.
- Document restore procedures and test periodically.

## Administrative Safeguards
- Perform and document a HIPAA risk analysis for this system, covering threats to confidentiality, integrity, and availability.
- Draft and adopt policies: access control, incident response, change management, data retention/secure destruction, vulnerability management.
- Train workforce members on PHI handling and minimum necessary principles.
- Maintain vendor due diligence (e.g., Phaxio BAA, SOC2 reports where applicable).

## Physical Safeguards
- Secure data center/hosting environment. For on‑prem: locked server rooms, visitor controls. For cloud: select providers with appropriate attestations.

## Backend‑Specific Guidance

### Phaxio (Cloud)
- Required:
  - BAA with Phaxio before sending PHI.
  - HTTPS `PUBLIC_API_URL`, valid certificate.
  - `PHAXIO_VERIFY_SIGNATURE=true`.
  - Strong `API_KEY` and reverse proxy restrictions.
- Recommended:
  - Keep `PDF_TOKEN_TTL_MINUTES` small (e.g., 15–60 minutes).
  - Immediately delete PDFs after successful transmission unless retention policy requires otherwise.
  - Validate that `PHAXIO_STATUS_CALLBACK_URL` is reachable only over TLS.

### SIP/Asterisk (Self‑Hosted)
- T.38/UDPTL is not encrypted. Mitigations:
  - Use a site‑to‑site VPN/private interconnect to your SIP provider, or run Asterisk in a private data center with dedicated connectivity.
  - Strict firewall allows only necessary ports and only to/from provider IPs.
  - Use SIP TLS for signaling if supported by your provider; still keep media protected by VPN.
- Asterisk hardening:
  - Do not expose AMI externally. Bind to private networks only.
  - Use non‑default usernames, strong secrets, fail2ban/IDS.
  - Rotate credentials periodically. Log and alert on failed auth.

## MCP (AI Assistant) Considerations
- Stdio vs HTTP/SSE transports
  - Stdio (local): connects tools directly to desktop assistants without a network server. Convenient for individuals. Not generally used for provider‑side HIPAA workflows.
  - HTTP/SSE (server): network transports that can be authenticated (API key, OAuth2/JWT) and deployed under your security program. Use SSE+OAuth for provider‑side HIPAA workflows.
- File handling
  - For stdio, prefer `send_fax` with `filePath` to avoid embedding PHI as base64 in conversations.
  - For HTTP/SSE, tool inputs are JSON; base64 increases size and token exposure. Enforce auth and rate limits and avoid logging request bodies.
- Do not send PHI to LLMs or external services unless covered by a BAA and approved by policy. Faxbot’s MCP servers call your Faxbot API; they do not upload PHI to model providers.
- All MCP servers must require authentication where applicable:
  - REST API: `X-API-Key` for /fax endpoints.
  - MCP HTTP/SSE: `Authorization: Bearer <JWT>` verified against your OIDC JWKS.
- Serve MCP over TLS. Never log PHI (file content, rendered pages). Log only job IDs and metadata.

## Roles and Transport Choice (Practical Guidance)
- Healthcare providers (CE/BA): use HTTPS for API, `phaxio` with HMAC or `sinch` with auth; for MCP use SSE+OAuth or skip MCP and call REST/SDKs directly.
- Patients/individuals sending their own documents: HIPAA obligations differ; using local stdio MCP is generally acceptable. The receiving provider bears most compliance obligations upon receipt. Providers must still secure inbound faxes on their systems.

## Operational Checklist (Minimum)
- [ ] Signed BAA with Phaxio (if using cloud backend).
- [ ] TLS everywhere (HTTPS for public endpoints; VPN/private link for SIP media).
- [ ] API auth enabled (`API_KEY` set). Reverse proxy with IP allowlist + rate limiting.
- [ ] MCP auth enforced (OAuth2 Bearer required for HTTP/SSE MCP).
- [ ] Callback signature verification enabled (`PHAXIO_VERIFY_SIGNATURE=true`).
- [ ] Tokenized PDF access enabled with short TTL (`PDF_TOKEN_TTL_MINUTES`).
- [ ] Logs do not contain PHI; tokens redacted; job IDs only.
- [ ] Encrypted storage for DB and artifacts; backups configured.
- [ ] Data retention policy implemented (delete artifacts after N days or on success).
- [ ] Asterisk AMI not exposed; strong credentials; fail2ban.
- [ ] Risk analysis, policies, and training documented.

## Current Implementation Status (2025‑Q3)
- Implemented:
  - Multi‑key API auth with admin endpoints (create/list/revoke/rotate) and bootstrap env key.
  - `REQUIRE_API_KEY` enforcement flag for production.
  - Tokenized PDF access with equality check and TTL expiry.
  - Phaxio callback signature verification (HMAC‑SHA256).
  - AMI concurrency/backoff improvements; SIP dialplan emits granular results.
  - Docs for HTTPS, rate limiting, NAT/port‑forwarding.
- Gaps (operator‑dependent):
  - Encryption at rest (volume or DB) is operator‑managed.
  - Automated retention cleanup (cron/job) recommended (see below).
  - Centralized audit logging & alerting recommended.

## Remediation Plan & Roadmap
1) Automate artifact retention
- Add `ARTIFACT_TTL_DAYS` env with a daily cleanup job to purge PDFs/TIFFs older than TTL when job status is final.

2) Configurable audit logging
- Structured logs with job lifecycle events; optional sink to SIEM.

3) Optional hard fail on plain HTTP
- Reject `PUBLIC_API_URL` with `http://` in non‑local environments unless `ALLOW_INSECURE_PUBLIC_URL=true`.

4) Secrets management
- Guidance and examples for loading secrets from a vault (AWS/GCP/Azure) instead of env files.

5) Provider‑specific SIP hardening
- Example configs for TLS signaling and site‑to‑site VPN topologies.

## Example: Retention Cleanup (Operator)
- Create a cron or systemd timer to delete artifacts after N days:
```
# delete PDFs/TIFFs older than 7 days
find /path/to/faxdata -type f \( -name '*.pdf' -o -name '*.tiff' \) -mtime +7 -delete
```
- Ensure backups honor retention and secure destruction policies.

## Legal Notice
- This document does not constitute legal advice. HIPAA compliance depends on your specific implementation, vendor agreements, and organizational controls. Engage qualified counsel and security professionals.
