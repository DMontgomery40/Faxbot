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

## What is MCP?

MCP (Model Context Protocol) is a standard for connecting AI assistants to external tools and data sources. Faxbot provides MCP servers that expose fax-sending capabilities to AI assistants.

## Available Integrations

- **Claude Desktop**: Use stdio transport for local desktop integration
- **Cursor**: Built-in MCP support with stdio transport  
- **Web Applications**: HTTP transport for cloud-based AI assistants
- **Enterprise**: SSE + OAuth2 transport for secure, authenticated access

## Transport Options

| Transport | Best For | Authentication | Complexity |
|-----------|----------|----------------|------------|
| **stdio** | Desktop AI (Claude, Cursor) | API key | Low |
| **HTTP** | Web apps, cloud AI | API key | Medium |
| **SSE+OAuth** | Enterprise, HIPAA environments | JWT/OAuth2 | High |

## Language Options

Faxbot provides MCP servers in both Node.js and Python with identical functionality. Choose based on your environment preferences.
