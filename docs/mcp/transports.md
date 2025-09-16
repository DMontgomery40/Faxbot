---
layout: default
title: Transports
parent: MCP Integration
nav_order: 3
permalink: /mcp/transports/
nav_exclude: false
---

# Transports

Stdio
- Best for desktop assistants; avoids base64 limits
- Prefer `filePath` for fidelity

HTTP (streamable)
- Node implementation at `/mcp`
- JSON body size limit ~16 MB
- Use API key for Faxbot REST calls from tools via environment (`API_KEY`)

SSE + OAuth2
- Node and Python servers require Bearer JWT with issuer/audience and JWKS
- Configure: `OAUTH_ISSUER`, `OAUTH_AUDIENCE`, optional `OAUTH_JWKS_URL`
- Always run behind TLS

Limits
- REST API raw file limit: `MAX_FILE_SIZE_MB` (default 10 MB)
- Node MCP JSON body limit: ~16 MB base64 payload
