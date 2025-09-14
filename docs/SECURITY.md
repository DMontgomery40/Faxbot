---
layout: default
title: Security
nav_order: 60
has_children: true
permalink: /security/
---

# Security

Configuration and guidance for HIPAA‑aligned deployments and OAuth/OIDC setup.

## Webhooks and Callbacks
- Outbound status (Phaxio): `POST /phaxio-callback` with HMAC verification. Keep callback URLs HTTPS.
- Inbound (cloud): `POST /phaxio-inbound` (HMAC) and `POST /sinch-inbound` (Basic and/or HMAC). Never disable signature checks in production.
- Inbound (self‑hosted): `POST /_internal/asterisk/inbound` with `X-Internal-Secret` on a private network.

## MCP Transports
- HTTP: protect with `MCP_HTTP_API_KEY` and strict `MCP_HTTP_CORS_ORIGIN`.
- SSE: require OAuth2/JWT; configure issuer/audience/JWKS; run behind TLS.
- WebSocket (Node): protect with `MCP_WS_API_KEY` (or `API_KEY`); run behind TLS or an authenticated proxy.

Recommended reading
- [Authentication (API Keys)](/Faxbot/AUTHENTICATION.html)
- [HIPAA Requirements](/Faxbot/HIPAA_REQUIREMENTS.html)
- [OAuth/OIDC Setup](/Faxbot/OAUTH_SETUP.html)
- [Compliance Overview (faxbot.net)](https://faxbot.net/compliance/)
- [Business Associate Agreement (PDF)](https://faxbot.net/compliance/business-associate-agreement.pdf)


