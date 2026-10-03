
# HIPAA Requirements

!!! warning "Not legal advice"
    This page is a technical guide and checklist for engineers and operators.  
    Always consult your compliance team and counsel. You (the operator) are responsible for implementing and documenting controls and for a formal risk analysis.

[:material-shield-lock: Security](security/index.md){ .md-button }
[:material-lan: Network & Transports](security/network.md){ .md-button }
[:material-key: Authentication](security/authentication.md){ .md-button }

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

!!! info "Authoritative sources"
    - HHS HIPAA Security Rule (overview): https://www.hhs.gov/hipaa/for-professionals/security/index.html  
    - Technical safeguards (45 CFR §164.312): https://www.ecfr.gov/current/title-45/subtitle-A/subchapter-C/part-164/subpart-C/section-164.312  
    - General rules (45 CFR §164.306): https://www.ecfr.gov/current/title-45/subtitle-A/subchapter-C/part-164/subpart-C/section-164.306  
    - Business Associate contracts (sample provisions): https://www.hhs.gov/hipaa/for-professionals/covered-entities/sample-business-associate-agreement-provisions/index.html  
    - Cloud computing guidance (OCR): https://www.hhs.gov/hipaa/for-professionals/special-topics/cloud-computing/index.html  
    - TLS configuration guidance (NIST SP 800‑52r2): https://csrc.nist.gov/pubs/sp/800/52/r2/final
Implement the following as minimum controls:

1) Transport security (164.312(e) Transmission Security)
- Public API must be served over HTTPS. Use TLS certs from a reputable CA.
- For Phaxio backend:
  - `PUBLIC_API_URL` must be HTTPS in production.
  - For outbound status callbacks, set the distinct account `PHAXIO_CALLBACK_TOKEN` and keep `PHAXIO_VERIFY_SIGNATURE=true`. `X-Phaxio-Signature` uses lowercase hexadecimal HMAC-SHA1 over the exact captured URL/query, stably name-sorted form fields and file-part SHA1 digests; see [the outbound contract](setup/webhooks.md#outbound-status-phaxio). The send API secret is not a callback token. A job captured with verification disabled rejects callback updates; captured-account status polling continues when a provider fax ID is known.
- For SIP backend:
  - SIP signaling should use TLS if supported by your provider; media (T.38 over UDPTL) is typically not encrypted. Mitigate with a site‑to‑site VPN/private interconnect to your SIP provider and strict firewalling.
  - Never expose AMI (5038/tcp) to the public internet.
  - Mapping: HIPAA Transmission Security (integrity and encryption) → use TLS/VPN for signaling/transport; T.38 media typically requires network isolation or VPN.

2) Access control (164.312(a)(1))
- Require API key on all /fax and /fax/{id} calls (`X-API-Key`). Do not run with blank `API_KEY` in production.
- Restrict inbound traffic with a reverse proxy: IP allowlists and rate limiting.
- Rotate credentials and set a strong AMI password. Do not use `changeme`.

3) Data minimization & confidentiality (164.306(a); 164.312(c)(1))
- Do not log PHI. Ensure request bodies (PDF/TXT) and rendered content are never logged.
- Faxbot redacts tokenized PDF URLs from logs.
- Tokenized PDF access:
  - The server issues a per‑job, random `pdf_token` with a short TTL (`PDF_TOKEN_TTL_MINUTES`, default 60). The `/fax/{job_id}/pdf` endpoint requires exact token equality and enforces expiry.
  - Keep TTL as short as operationally feasible.

4) Storage security (at rest)
- Store database and artifacts on encrypted volumes or use a managed, encrypted database. SQLite is acceptable only if disk encryption and backups are in place.
- Separate storage for development vs production. Limit admin access and use MFA on hosts.
- Data retention policy: delete PDFs/TIFFs after transmission completes and your minimum retention requirement is satisfied.

5) Integrity & auditing (164.312(c)(1), 164.312(b))
- Maintain audit logs of access to `/fax/{job_id}/pdf`, job creation, and status changes. No PHI in logs; use job IDs and metadata only.
- Time synchronize servers (NTP) for accurate audit trails.

6) Availability & recovery (164.306(a))
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
  - Account Callback Token captured as `PHAXIO_CALLBACK_TOKEN` for outbound callbacks.
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
- Do not send PHI to LLMs or external services unless covered by a BAA and approved by policy. Faxbot's MCP servers call your Faxbot API; they do not upload PHI to model providers.
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
- [ ] Outbound callback token configured (`PHAXIO_CALLBACK_TOKEN`) and verification enabled (`PHAXIO_VERIFY_SIGNATURE=true`).
- [ ] Tokenized PDF access enabled with short TTL (`PDF_TOKEN_TTL_MINUTES`).
- [ ] Logs do not contain PHI; tokens redacted; job IDs only.
- [ ] Encrypted storage for DB and artifacts; backups configured.
- [ ] Data retention policy implemented (delete artifacts after N days or on success).
- [ ] Asterisk AMI not exposed; strong credentials; fail2ban.
- [ ] Risk analysis, policies, and training documented.

## Current implementation and operation

Use the maintained guides for [authentication](security/authentication.md), [access control](security/access-control.md), [settings and retention](admin-console/settings.md), [provider callbacks](setup/webhooks.md), and [MCP transport security](mcp/transports.md). The historical implementation assessment and remediation plan have been archived; they do not describe the current release.

## Legal Notice
- This document does not constitute legal advice. HIPAA compliance depends on your specific implementation, vendor agreements, and organizational controls. Engage qualified counsel and security professionals.
