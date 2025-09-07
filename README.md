# Faxbot

### NOTE: This is still in production, pre-alpha stage at this point, but the self-hosted methods have been tested and function reliably, so far. 

Simple fax-sending API with AI integration. Choose your backend:

## Quick Start Options

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

- Node MCP servers live in `node_mcp/` (stdio, HTTP, SSE+OAuth).
- Legacy servers remain under `api/` and Python `python_mcp/`.
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
- [API Reference](docs/API_REFERENCE.md) — Endpoints and examples
- [Troubleshooting](docs/TROUBLESHOOTING.md) — Common issues
- [HIPAA Requirements](HIPAA_REQUIREMENTS.md) — Security, BAAs, and compliance checklist
- [Images vs Text PDFs](docs/IMAGES_AND_PDFS.md) — The right way to fax scans/photos

## Notes
- Send-only. Receiving is out of scope.
- Set `FAX_BACKEND` to `phaxio` (cloud) or `sip` (self-hosted).
- Use `X-API-Key` for auth; secure behind a reverse proxy for rate limiting.

**Demo**
<video src="assets/faxbot_demo.mov" width="100%" autoplay loop muted playsinline controls>
  <a href="assets/faxbot_demo.mov">Download the demo video</a>
  (Your browser does not support embedded videos.)
 </video>
