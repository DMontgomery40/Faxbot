# TODO

This is a focused, prioritized backlog based on the latest audit.

## High Priority
- [COMPLETED] Secure PDF token validation for cloud fetch (Phaxio)
  - Added `pdf_token` and `pdf_token_expires_at` columns
  - Strict equality check + expiry enforced (default 60 mins via `PDF_TOKEN_TTL_MINUTES`)
  - Docs and `.env.example` updated
- [COMPLETED] Verify Phaxio webhook authenticity
  - HMAC‑SHA256 verification of raw body via `X-Phaxio-Signature` using `PHAXIO_API_SECRET`
  - Enabled by default via `PHAXIO_VERIFY_SIGNATURE=true`
  - Docs updated
- [COMPLETED] AMI reconnect concurrency safety
  - Added connection lock; background reconnect from read loop
  - Keeps exponential backoff
- [COMPLETED] Asterisk AMI username templating
  - `manager.conf.template` now uses `${ASTERISK_AMI_USERNAME}` for the section name
  - Docs updated to call out the alignment with API env
- [COMPLETED] Redact sensitive tokens from logs
  - Do not log `pdf_url` tokens
  - Ensure all logs contain job IDs, not secrets
- [COMPLETED] Streamable HTTP MCP server
  - Implemented `/mcp` POST/GET/DELETE with `StreamableHTTPServerTransport`
  - Session management and SSE notifications supported
  - Docs updated; scripts and Makefile remain valid

## Medium Priority
- [COMPLETED] Skip TIFF generation for Phaxio path
  - Do not convert to TIFF when backend is `phaxio`; pages set by callback
- [COMPLETED] Source of truth for pages
  - Provider callback overwrites local estimates; documented
- [COMPLETED] Public URL defaults and docs
  - Startup warns when `PUBLIC_API_URL` is plain HTTP (non-local)
  - Docs recommend HTTPS for production
- [COMPLETED] Rate limiting and auth posture
  - Startup warns when `API_KEY` is unset and faxing is enabled
  - Added reverse proxy examples (Nginx/Caddy) to Troubleshooting

## Low Priority
- [COMPLETED] Phone number normalization
  - Documented E.164 preference and best‑effort normalization note
- MCP example configs
  - Treat `api/configs/*.json` as examples; rely on `setup-mcp.js` to generate paths
- [COMPLETED] Ghostscript flags
  - Removed redundant compression flag for tiffg4
- File retention
  - Add TTL cleanup for `orig`, `pdf`, and `tiff` artifacts
  - Document storage footprint and retention controls
- [COMPLETED] CI tests and scripts
  - GitHub Actions workflow added to run tests

## Documentation Improvements
- Port forwarding appendix for novices (router examples/screenshots)
- Asterisk template walkthrough with env examples per provider
- “Data persistence” note: mount DB/file volumes for durability
- [ADDED] HIPAA requirements document and operator checklist
  - Status: Completed (HIPAA_REQUIREMENTS.md)
- [COMPLETED] Audit logging docs
  - Added optional SIEM sink configuration and events list
- [COMPLETED] Provider TLS/VPN examples
  - Added PJSIP TLS and WireGuard sketches to SIP guide

---

# In Progress
- (none)
