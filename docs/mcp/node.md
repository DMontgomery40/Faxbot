---
layout: default
title: Node MCP
parent: MCP Integration
nav_order: 2
permalink: /mcp/node/
nav_exclude: false
---

# Node MCP

Stdio server
- Path: `node_mcp/src/servers/stdio.js`
- Start: `node node_mcp/src/servers/stdio.js`

HTTP (streamable)
- Path: `node_mcp/src/servers/http.js`
- Env: `MCP_HTTP_PORT` (default 3001)
- Start: `node node_mcp/src/servers/http.js`
- Endpoints: `POST /mcp`, `GET /mcp`, `DELETE /mcp`, `GET /health`

SSE (OAuth2)
- Path: `node_mcp/src/servers/sse.js`
- Env: `OAUTH_ISSUER`, `OAUTH_AUDIENCE`, optional `OAUTH_JWKS_URL`, `MCP_SSE_PORT`
- Start: `node node_mcp/src/servers/sse.js`
- Endpoints: `GET /sse`, `POST /messages`, `DELETE /messages`, `GET /health`

Tools
- `send_fax(to, filePath | fileContent+fileName[, fileType])`
- `get_fax_status(jobId)`

See AI Integration for Compose startup: {{ site.baseurl }}/ai-integration/
