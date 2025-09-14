---
layout: default
title: Deployment
nav_order: 50
permalink: /deployment/
---

# Deployment

Services
- `api`: FastAPI service (required)
- `asterisk`: SIP/Asterisk backend (only for `FAX_BACKEND=sip`)
- `faxbot-mcp`: MCP server (optional)

Ports
- `8080`: API
- `3001`: MCP HTTP (Node)
- `3002`: MCP SSE (Node)
- `3003`: MCP SSE (Python)
- SIP/Asterisk only: `5060` (SIP), `5038` (AMI internal), `4000-4999` (UDPTL)

Storage and database
- `FAX_DATA_DIR` for PDFs/TIFFs (default `./faxdata`)
- SQLite for dev; use Postgres in production (`DATABASE_URL`)
- S3/S3‑compatible for inbound artifacts; for SSE‑KMS see AWS docs below

Public URL and TLS
- Set `PUBLIC_API_URL` to your HTTPS endpoint
- `ENFORCE_PUBLIC_HTTPS=true` for production with cloud backends
- For quick testing, use a tunnel:
  - Cloudflare Tunnel: https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/do-more-with-tunnels/trycloudflare/
  - ngrok (HTTP): https://ngrok.com/docs/guides/http/

Artifacts cleanup
- `ARTIFACT_TTL_DAYS` to delete old PDFs/TIFFs after completion
- `CLEANUP_INTERVAL_MINUTES` controls sweep frequency

Security
- Set `API_KEY` and require `X-API-Key` header
- Never expose AMI (5038) publicly
- Use HTTPS for callbacks and public endpoints

References
- AWS S3 SSE‑KMS: https://docs.aws.amazon.com/AmazonS3/latest/userguide/UsingKMSEncryption.html
- Third‑Party docs: [/third-party/](/Faxbot/third-party/)
