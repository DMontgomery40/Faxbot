# Faxbot


<p align="center">
  <img src="docs/assets/images/faxbot_full_logo.png" alt="Faxbot logo" width="100%" />
</p>

The first and only open-source, self-hostable fax API. Send faxes with a single function call.

Yes, this repo might look overwhelming at first glance—that's only because Faxbot supports multiple backends (cloud and self-hosted), several MCP transport options for AI integration, and HIPAA-compliant security configurations. Most users will only need one path through this complexity.

**Core function:** `send_fax(phone_number, pdf_file)` → Done.

To our knowledge, no other open-source project combines:
- Modern REST API for fax transmission
- Multiple backend options (Phaxio cloud, Sinch cloud, self-hosted SIP/Asterisk)
- AI assistant integration via MCP (Model Context Protocol)
- HIPAA compliance features for healthcare PHI
- Developer SDKs for Node.js and Python

Questions? Issues? **Please don't hesitate to reach out.** See [CONTRIBUTING.md](CONTRIBUTING.md) for the best way to get help.

## Quick Start Options

### Docker Compose (API + optional MCP)
- Copy and edit `.env` (or start from `.env.example`).
- Run the API:
```
docker compose up -d --build api
```
- Optional MCP (HTTP, port 3001):
```
docker compose --profile mcp up -d --build faxbot-mcp
# or: make mcp-up
```
- Optional MCP SSE (OAuth2/JWT, port 3002):
```
export OAUTH_ISSUER=https://YOUR_ISSUER
export OAUTH_AUDIENCE=faxbot-mcp
export OAUTH_JWKS_URL=https://YOUR_ISSUER/.well-known/jwks.json
docker compose --profile mcp up -d --build faxbot-mcp-sse
# or: make mcp-sse-up
```
- Check health: `curl http://localhost:8080/health`
- MCP HTTP health: `curl http://localhost:3001/health`
 - MCP SSE health: `curl http://localhost:3002/health`

### Option 1: Phaxio (Recommended for Most Users)
- 5-minute setup
- No telephony knowledge required
- Pay per fax (cloud)

[→ Phaxio Setup Guide](docs/PHAXIO_SETUP.md)

### Option 2: Sinch Fax API v3 (Cloud)
- Direct upload model (no PUBLIC_API_URL fetch)
- Works with “Phaxio by Sinch” accounts
- Requires Project ID + API key/secret

[→ Sinch Setup Guide](docs/SINCH_SETUP.md)

### Option 3: Self-Hosted SIP/Asterisk
- Full control
- No per-fax cloud charges
- Requires SIP trunk and T.38 knowledge

[→ SIP Setup Guide](docs/SIP_SETUP.md)

## AI Assistant Integration
[→ MCP Integration Guide](docs/MCP_INTEGRATION.md)

- Node MCP servers live in `node_mcp/` (stdio, HTTP, SSE+OAuth). Python MCP servers live in `python_mcp/`.
- OAuth2‑protected SSE MCP servers are available in both Node and Python.

Important file-type note
- Faxbot accepts only PDF and TXT. If you have images (PNG/JPG), convert them to PDF before sending.
- Quick conversions:
  - macOS Preview: File → Export As… → PDF
  - macOS CLI: `sips -s format pdf "in.png" --out "out.pdf"`
  - Linux: `img2pdf in.png -o out.pdf` or `magick convert in.png out.pdf`
  - Windows: open image → Print → “Microsoft Print to PDF”.

Stdio “just works” tip
- For desktop assistants, prefer the Node or Python stdio MCP and call `send_fax` with `filePath` to your local PDF/TXT. This bypasses base64 and avoids token limits.

## Client SDKs
- Python: `pip install faxbot`
- Node.js: `npm install faxbot`

[→ SDK Usage Guide](docs/SDKS.md)

## Documentation
Core guides
- [MCP Integration](docs/MCP_INTEGRATION.md) — Claude/Cursor stdio, HTTP, SSE (Node + Python)
- [API Reference](docs/API_REFERENCE.md) — Endpoints and examples
- [Client SDKs](docs/SDKS.md) — Python and Node SDK usage
 - MCP Inspector: use `docker compose --profile mcp up -d mcp-inspector` and open http://localhost:6274 to explore tools/resources/prompts

Backends
- [Phaxio Setup](docs/PHAXIO_SETUP.md) — Cloud (tokenized PDF URL + HMAC webhook)
- [Sinch Setup](docs/SINCH_SETUP.md) — Cloud direct upload (v3 API)
- [SIP/Asterisk Setup](docs/SIP_SETUP.md) — Self-hosted T.38

Security & compliance
- [HIPAA Requirements](HIPAA_REQUIREMENTS.md) — Security, BAAs, and compliance checklist
- [OAuth/OIDC Setup](docs/OAUTH_SETUP.md) — Configure SSE with Auth0, Okta, Azure AD, Google, Keycloak

File handling
- [Images vs Text PDFs](docs/IMAGES_AND_PDFS.md) — The right way to fax scans/photos

Advanced
- [Phaxio End-to-End Test](docs/PHAXIO_E2E_TEST.md) — Simulated callback flow for local testing

## Notes
- Send-only. Receiving is out of scope.
- Set `FAX_BACKEND` to `phaxio` (cloud) or `sip` (self-hosted).
- Use `X-API-Key` for auth; secure behind a reverse proxy for rate limiting.

Demo
<video src="assets/faxbot_demo.mp4" width="100%" autoplay loop muted playsinline controls>
  <a href="assets/faxbot_demo.mp4">Watch the demo video</a>
  (Your browser or GitHub may not inline-play videos; use the link.)
</video>