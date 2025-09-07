---
layout: default
title: Home
nav_order: 1
description: "The first open-source, self-hostable fax API with AI integration"
permalink: /
---

# Faxbot

Simple fax-sending API with AI integration. Choose your backend:

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

[→ Phaxio Setup Guide](backends/phaxio-setup.html)

### Option 2: Sinch Fax API v3 (Cloud)
- Direct upload model (no PUBLIC_API_URL fetch)
- Works with "Phaxio by Sinch" accounts
- Requires Project ID + API key/secret

[→ Sinch Setup Guide](backends/sinch-setup.html)

### Option 3: Self-Hosted SIP/Asterisk
- Full control
- No per-fax cloud charges
- Requires SIP trunk and T.38 knowledge

[→ SIP Setup Guide](backends/sip-setup.html)

## AI Assistant Integration
[→ MCP Integration Guide](ai-integration/mcp-integration.html)

- Node MCP servers live in `node_mcp/` (stdio, HTTP, SSE+OAuth).
- Legacy servers remain under `api/` and Python `python_mcp/`.
- OAuth2‑protected SSE MCP servers are available in both Node and Python.

Important file-type note
- Faxbot accepts only PDF and TXT. If you have images (PNG/JPG), convert them to PDF before sending.
- Quick conversions:
  - macOS Preview: File → Export As… → PDF
  - macOS CLI: `sips -s format pdf "in.png" --out "out.pdf"`
  - Linux: `img2pdf in.png -o out.pdf` or `magick convert in.png out.pdf`
  - Windows: open image → Print → "Microsoft Print to PDF".

Stdio "just works" tip
- For desktop assistants, prefer the Node or Python stdio MCP and call `send_fax` with `filePath` to your local PDF/TXT. This bypasses base64 and avoids token limits.

## Client SDKs
- Python: `pip install faxbot`
- Node.js: `npm install faxbot`

[→ SDK Usage Guide](development/sdks.html)

## Documentation
Core guides
- [MCP Integration](ai-integration/mcp-integration.html) — Claude/Cursor stdio, HTTP, SSE (Node + Python)
- [API Reference](development/api-reference.html) — Endpoints and examples
- [Client SDKs](development/sdks.html) — Python and Node SDK usage
 - MCP Inspector: use `docker compose --profile mcp up -d mcp-inspector` and open http://localhost:6274 to explore tools/resources/prompts

Backends
- [Phaxio Setup](backends/phaxio-setup.html) — Cloud (tokenized PDF URL + HMAC webhook)
- [Sinch Setup](backends/sinch-setup.html) — Cloud direct upload (v3 API)
- [SIP/Asterisk Setup](backends/sip-setup.html) — Self-hosted T.38

Security & compliance
- [HIPAA Requirements](security/hipaa-requirements.html) — Security, BAAs, and compliance checklist
- [OAuth/OIDC Setup](security/oauth-setup.html) — Configure SSE with Auth0, Okta, Azure AD, Google, Keycloak

File handling
- [Images vs Text PDFs](backends/images-and-pdfs.html) — The right way to fax scans/photos

Advanced
- [Phaxio End-to-End Test](development/phaxio-e2e-test.html) — Simulated callback flow for local testing

## Notes
- Send-only. Receiving is out of scope.
- Set `FAX_BACKEND` to `phaxio` (cloud) or `sip` (self-hosted).
- Use `X-API-Key` for auth; secure behind a reverse proxy for rate limiting.

Demo
<video src="assets/images/faxbot_demo.mp4" width="100%" autoplay loop muted playsinline controls>
  <a href="assets/images/faxbot_demo.mp4">Watch the demo video</a>
  (Your browser or GitHub may not inline-play videos; use the link.)
</video>
