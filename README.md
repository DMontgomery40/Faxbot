# Faxbot

### NOTE: This is still in production, pre-alpha stage at this point, but the self-hosted methods have been tested and function reliably, so far. 

Simple fax-sending API with AI integration. Choose your backend:

## Quick Start Options

### Option 1: Phaxio (Recommended for Most Users)
- 5-minute setup
- No telephony knowledge required
- Pay per fax (cloud)

[→ Phaxio Setup Guide](docs/PHAXIO_SETUP.md)

### Option 2: Self-Hosted SIP/Asterisk
- Full control
- No per-fax cloud charges
- Requires SIP trunk and T.38 knowledge

[→ SIP Setup Guide](docs/SIP_SETUP.md)

## AI Assistant Integration
[→ MCP Integration Guide](docs/MCP_INTEGRATION.md)

- Recommended (OCR, avoids base64): use the new Node MCP servers in `node_mcp/` and the `faxbot_pdf` prompt to extract PDF text locally and send as TXT fax.
  - `cd node_mcp && npm install && ./scripts/start-stdio.sh`
  - Env: `FAX_API_URL`, `API_KEY`, optional `MAX_TEXT_SIZE`
- Legacy servers remain under `api/` (`start-mcp.sh`, `start-mcp-http.sh`) and Python `python_mcp/`.
- OAuth2‑protected SSE MCP servers are available in both Node and Python — see the SSE sections in the MCP guide.

## Client SDKs
- Python: `pip install faxbot`
- Node.js: `npm install faxbot`

[→ SDK Usage Guide](docs/SDKS.md)

## Documentation
- [API Reference](docs/API_REFERENCE.md) — Endpoints and examples
- [Troubleshooting](docs/TROUBLESHOOTING.md) — Common issues
- [HIPAA Requirements](HIPAA_REQUIREMENTS.md) — Security, BAAs, and compliance checklist

## Notes
- Send-only. Receiving is out of scope.
- Set `FAX_BACKEND` to `phaxio` (cloud) or `sip` (self-hosted).
- Use `X-API-Key` for auth; secure behind a reverse proxy for rate limiting.
