---
layout: default
title: Documo mFax Setup
parent: Backends
nav_order: 3
permalink: /backends/documo-setup.html
---

# Documo mFax Setup

Beginner‑friendly cloud backend using Documo (mFax). Direct upload — no domain or tunnel required to send.

When to use
- You want a quick send path without setting up a public URL.
- You have, or can create, an mFax/Documo account and API key.

Quick links
- Sign up / pricing: https://www.mfax.io/pricing
- Docs: https://docs.documo.com

Environment
```
FAX_BACKEND=documo
DOCUMO_API_KEY=your_documo_api_key
# Optional: sandbox for testing
# DOCUMO_SANDBOX=true
# Optional: override base URL
# DOCUMO_BASE_URL=https://api.documo.com

# General
API_KEY=your_secure_api_key   # optional but recommended (X-API-Key to your Faxbot API)
```

Send a fax (curl)
```
curl -X POST http://localhost:8080/fax   -H "X-API-Key: $API_KEY"   -F to=+15551234567   -F file=@./example.pdf
```

Notes
- Faxbot uploads your PDF directly via `POST /v1/faxes` with your API key; no callback URL is needed to send.
- Status in Faxbot starts as queued/in_progress and updates based on provider responses. You can poll `GET /fax/{id}`.

Troubleshooting
- 401: invalid API key to your Faxbot API (set `API_KEY` and send `X-API-Key`).
- Provider auth errors: verify `DOCUMO_API_KEY` and whether sandbox is on/off.
- Only PDF/TXT files are accepted by Faxbot’s REST API; convert images to PDF first.

Official References
- API docs: https://docs.documo.com
