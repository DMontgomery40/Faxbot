---
layout: default
title: Python MCP
parent: MCP Integration
nav_order: 1
permalink: /mcp/python/
---

# Python MCP

Stdio server
- Path: `python_mcp/stdio_server.py`
- Env: `FAX_API_URL`, optional `API_KEY`
- Start:
  - `python -m venv .venv && source .venv/bin/activate`
  - `pip install -r requirements.txt`
  - `export FAX_API_URL=http://localhost:8080`
  - `python stdio_server.py`

SSE server (OAuth2/JWT)
- Path: `python_mcp/server.py`
- Env:
  - `OAUTH_ISSUER`, `OAUTH_AUDIENCE`, optional `OAUTH_JWKS_URL`
  - `FAX_API_URL`, optional `API_KEY`
- Start: `uvicorn server:app --host 0.0.0.0 --port 3003`

Tools
- `send_fax(to, filePath | fileContent+fileName[, fileType])`
- `get_fax_status(jobId)`

