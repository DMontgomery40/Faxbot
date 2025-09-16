---
layout: default
title: AI Integration
nav_order: 6
has_children: true
permalink: /ai-integration/
---

<div class="home-hero">
  <img src="{{ site.baseurl }}/assets/images/faxbot_full_logo.png" alt="Faxbot logo" />
</div>

# AI Assistant Integration

Faxbot integrates with AI assistants via the Model Context Protocol (MCP) so you can say “fax this document to my doctor.”

<video src="{{ site.baseurl }}/assets/images/faxbot_demo.mp4" width="100%" autoplay loop muted playsinline controls>
  <a href="{{ site.baseurl }}/assets/images/faxbot_demo.mp4">Watch the MCP demo video</a>
</video>

## Transports (No-CLI Startup)

Use Docker Compose profiles to run MCP servers without manual Node/Python commands. Ensure the Faxbot API is running first (`http://localhost:8080`).

- Start Node HTTP server (port 3001): `docker compose --profile mcp up -d faxbot-mcp`
- Start Node SSE+OAuth (port 3002): `docker compose --profile mcp up -d faxbot-mcp-sse`
- Start Python SSE+OAuth (port 3003): `docker compose --profile mcp up -d faxbot-mcp-py-sse`

Auth
- REST API: `X-API-Key` if required by your deployment
- SSE+OAuth: Bearer JWT validated against your OIDC issuer and JWKS

File handling
- Stdio transports support `filePath` (no base64). For HTTP/SSE, send base64 in JSON but keep under the 16 MB JSON limit (API raw limit is 10 MB).

## What is MCP?

MCP (Model Context Protocol) is a standard for connecting AI assistants to external tools and data sources. Faxbot provides MCP servers that expose fax-sending capabilities to AI assistants.

## Available Integrations

- Claude Desktop or Cursor: stdio transport (desktop) for the smoothest local experience
- Web apps: HTTP transport
- Enterprise/HIPAA: SSE + OAuth2/JWT

## Transport Options

| Transport | Best For | Authentication | Complexity |
|-----------|----------|----------------|------------|
| stdio | Desktop AI (Claude, Cursor) | API key | Low |
| HTTP | Web apps, cloud AI | API key | Medium |
| SSE+OAuth | Enterprise, HIPAA environments | JWT/OAuth2 | High |

## Language Options

Faxbot provides MCP servers in both Node.js and Python with identical functionality. Choose based on your environment preferences. Compose services are provided for both stacks.
## Advanced (Direct Commands)

For developers who prefer direct startup without Compose, use these commands from the repo root:

- Node stdio: `FAX_API_URL=http://localhost:8080 API_KEY=$API_KEY node node_mcp/src/servers/stdio.js`
- Node HTTP: `MCP_HTTP_PORT=3001 FAX_API_URL=http://localhost:8080 API_KEY=$API_KEY node node_mcp/src/servers/http.js`
- Node SSE: `MCP_SSE_PORT=3002 OAUTH_ISSUER=... OAUTH_AUDIENCE=faxbot-mcp OAUTH_JWKS_URL=... FAX_API_URL=http://localhost:8080 API_KEY=$API_KEY node node_mcp/src/servers/sse.js`
- Python SSE: `cd python_mcp && uvicorn server:app --host 0.0.0.0 --port 3003`

See also:
- Node reference: {{ site.baseurl }}/mcp/node/
- Python reference: {{ site.baseurl }}/mcp/python/
