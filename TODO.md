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
- Asterisk AMI username templating
  - Use `${ASTERISK_AMI_USERNAME}` as the section name in `manager.conf.template`
  - Keep credentials aligned between API env and Asterisk
- [COMPLETED] Redact sensitive tokens from logs
  - Do not log `pdf_url` tokens
  - Ensure all logs contain job IDs, not secrets

## Medium Priority
- Skip TIFF generation for Phaxio path
  - Avoid `pdf_to_tiff` when backend is `phaxio` to save CPU/IO
  - Page count can be filled by provider callback
- Source of truth for pages
  - Ensure provider callback updates overwrite any local estimates
  - Document this behavior
- Public URL defaults and docs
  - Prefer HTTPS for `PUBLIC_API_URL` in docs
  - Warn on startup when using localhost in non‑dev
- Rate limiting and auth posture
  - Warn if `API_KEY` is unset and `FAX_DISABLED=false`
  - Add reverse proxy (nginx/Caddy) examples for IP allowlist + rate limits

## Low Priority
- Phone number normalization
  - Document E.164 expectation; avoid guessing country codes
- MCP example configs
  - Treat `api/configs/*.json` as examples; rely on `setup-mcp.js` to generate paths
- Ghostscript flags
  - Clean up redundant options (e.g., lzw with tiffg4)
- File retention
  - Add TTL cleanup for `orig`, `pdf`, and `tiff` artifacts
  - Document storage footprint and retention controls
- CI tests and scripts
  - Add GH Actions to run `pytest -q` with `FAX_DISABLED=true`

## Documentation Improvements
- Port forwarding appendix for novices (router examples/screenshots)
- Asterisk template walkthrough with env examples per provider
- “Data persistence” note: mount DB/file volumes for durability

---

# In Progress
- Implement Asterisk AMI username templating
