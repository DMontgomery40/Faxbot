---
layout: default
title: AI Integration
nav_order: 4
has_children: true
permalink: /ai-integration/
---

<div class="home-hero">
  <img src="{{ site.baseurl }}/docs/assets/images/faxbot_full_logo.png" alt="Faxbot logo" />
</div>

# AI Integration

Faxbot integrates with AI assistants through the Model Context Protocol (MCP), allowing you to send faxes using natural language commands like "fax this document to my doctor."

## Transports — Quick Start

Pick one transport based on your environment. All require the Faxbot API to be running (default `http://localhost:8080`) and an API key if enabled.

1) stdio (desktop assistants)
```
cd node_mcp
FAX_API_URL=http://localhost:8080 API_KEY=$API_KEY node src/servers/stdio.js
```
Use `filePath` in tools to send local PDFs/TXTs without base64.

2) HTTP (web/cloud)
```
cd node_mcp
MCP_HTTP_PORT=3001 FAX_API_URL=http://localhost:8080 API_KEY=$API_KEY node src/servers/http.js
```
Send `X-API-Key` on requests. Restrict `MCP_HTTP_CORS_ORIGIN` in production.

3) SSE (enterprise)
```
cd node_mcp
MCP_SSE_PORT=3002 OAUTH_ISSUER=... OAUTH_AUDIENCE=faxbot-mcp OAUTH_JWKS_URL=... \
FAX_API_URL=http://localhost:8080 API_KEY=$API_KEY node src/servers/sse.js
```
Protect with OAuth2/JWT (required for HIPAA scenarios).

4) WebSocket (realtime)
```
cd node_mcp
MCP_WS_PORT=3004 API_KEY=$API_KEY node src/servers/ws.js
```
Connect to `ws://localhost:3004` (optionally add `?key=$API_KEY`).
