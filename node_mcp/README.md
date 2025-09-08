# Faxbot Node MCP

Node-based MCP servers for Faxbot.

Features:
- Stdio, Streamable HTTP, and SSE+OAuth transports
- Tools: send_fax, get_fax_status

## Install

From repo root:

```
cd node_mcp
npm install
```

Environment variables:
- `FAX_API_URL` (default `http://localhost:8080`)
- `API_KEY` (Faxbot API key)
- SSE only: `OAUTH_ISSUER`, `OAUTH_AUDIENCE`, optional `OAUTH_JWKS_URL`

## Run

```
# Stdio
./scripts/start-stdio.sh

# HTTP (Streamable)
./scripts/start-http.sh

# SSE + OAuth
./scripts/start-sse.sh
```

## Tools

- `send_fax(to, filePath?, fileContent?, fileName?, fileType?)`
- `get_fax_status(jobId)`

## Notes
- For local files, tools accept a `filePath` parameter (preferred). Base64 is still supported for compatibility.
