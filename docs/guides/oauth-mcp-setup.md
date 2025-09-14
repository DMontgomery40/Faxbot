---
layout: default
title: Secure MCP SSE with OAuth2
parent: Guides
nav_order: 3
permalink: /guides/oauth-mcp-setup/
---

# Secure MCP SSE with OAuth2

Node SSE
- Set `OAUTH_ISSUER`, `OAUTH_AUDIENCE`, optional `OAUTH_JWKS_URL`
- Start: `node node_mcp/src/servers/sse.js`
- Health: `GET /health`

Python SSE
- Same env; start with `uvicorn python_mcp/server:app --port 3003`

Assistant config
- Provide the SSE URL, include Bearer token on requests
- Keep tokens short‑lived and scoped

Security checklist
- TLS termination in front of the server
- Validate issuer and audience
- Prefer private networks and least privilege

Links
- [MCP overview](/Faxbot/mcp/)
- [Security: OAuth/OIDC](/Faxbot/security/oauth/)

