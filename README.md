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

- Quick start: use the scripts in `api/scripts` (`start-mcp.sh`, `start-mcp-http.sh`) or `make mcp-stdio` / `make mcp-http`.

## Documentation
- [API Reference](docs/API_REFERENCE.md) — Endpoints and examples
- [Troubleshooting](docs/TROUBLESHOOTING.md) — Common issues

## Notes
- Send-only. Receiving is out of scope.
- Set `FAX_BACKEND` to `phaxio` (cloud) or `sip` (self-hosted).
- Use `X-API-Key` for auth; secure behind a reverse proxy for rate limiting.
