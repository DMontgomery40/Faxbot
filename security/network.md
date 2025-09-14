---
layout: default
title: Network & Transports
parent: Security
nav_order: 10
permalink: /security/network.html
---

# Network & Transports

Guidance for securing MCP transports and webhooks when running Faxbot in production.

## MCP Transports

- HTTP (Node MCP)
  - Port: 3001 (default)
  - Protect with `MCP_HTTP_API_KEY`; set strict `MCP_HTTP_CORS_ORIGIN` (no `*` when credentials).
  - Run behind TLS via reverse proxy; add IP allowlists and rate limits where appropriate.

- SSE (Node/Python MCP)
  - Ports: 3002 (Node), 3003 (Python)
  - Require OAuth2/JWT in production. Configure `OAUTH_ISSUER`, `OAUTH_AUDIENCE`, and (optionally) `OAUTH_JWKS_URL`.
  - Run behind TLS; validate tokens against your IdP; set short TTLs.

- WebSocket (Node MCP)
  - Port: 3004 (default)
  - Protect with `MCP_WS_API_KEY` (or reuse `API_KEY`) and run behind TLS or an authenticated proxy.
  - Use only for trusted clients or internal networks.

## Webhooks & Callbacks

- Phaxio (outbound status)
  - Endpoint: `POST /phaxio-callback`
  - Signature: `X-Phaxio-Signature` (HMAC-SHA256 of raw body using `PHAXIO_API_SECRET`)
  - Always use HTTPS public URLs; avoid exposing staging/test endpoints publicly.

- Phaxio (inbound)
  - Endpoint: `POST /phaxio-inbound`
  - Signature: `X-Phaxio-Signature` (HMAC-SHA256)

- Sinch (inbound)
  - Endpoint: `POST /sinch-inbound`
  - Basic auth: `SINCH_INBOUND_BASIC_USER/PASS`
  - HMAC: `X-Sinch-Signature` with `SINCH_INBOUND_HMAC_SECRET`

- SIP/Asterisk (inbound)
  - Endpoint: `POST /_internal/asterisk/inbound`
  - Header: `X-Internal-Secret: <ASTERISK_INBOUND_SECRET>`
  - Only accessible over private networks; do not expose publicly.

## Reverse Proxy Recommendations

- Enforce TLS; redirect HTTP→HTTPS.
- Set security headers (HSTS, CSP, X-Content-Type-Options, Referrer-Policy, X-Frame-Options, Permissions-Policy).
- Limit request sizes; apply rate limits and IP restrictions as needed.
- Do not log PHI; log IDs and generic metadata only.

