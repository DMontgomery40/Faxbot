# Python MCP

Built on the MCP Python SDK (`mcp` 2.3.0). Requires Python 3.11 or newer.

Install: `pip install -r python_mcp/requirements.txt`

Stdio server

- Path: `python_mcp/stdio_server.py`
- Env: `FAX_API_URL`, `API_KEY` (used for every tool call)
- Start: `python python_mcp/stdio_server.py`

Streamable HTTP server

- Path: `python_mcp/http_server.py`
- Start: `uvicorn http_server:app --host 0.0.0.0 --port 3004` (from `python_mcp/`)
- Endpoints: `POST /mcp`, `GET /health`

SSE server (compatibility transport)

- Path: `python_mcp/server.py`
- Start: `uvicorn server:app --host 0.0.0.0 --port 3003` (from `python_mcp/`)
- Endpoints: `GET /sse`, `POST /messages/`, `GET /health`

Both network servers read `FAX_API_URL`, optional `MCP_ALLOWED_HOSTS` and `MCP_ALLOWED_ORIGINS`, and for OAuth `OAUTH_ISSUER`, `OAUTH_AUDIENCE`, optional `OAUTH_JWKS_URL`, `MCP_OAUTH_SUBJECT_KEYS_FILE` and `MCP_RESOURCE_URL`. They never use `API_KEY`.

Embedded in the API

- `ENABLE_MCP_HTTP=true` serves Streamable HTTP at `/mcp/http/mcp`, with a health check at `/mcp/http/health`
- `ENABLE_MCP_SSE=true` serves SSE at `/mcp/sse/sse`, with a health check at `/mcp/sse/health`
- `REQUIRE_MCP_OAUTH=true` switches both to OAuth bearer tokens

The health checks need no credential and return `{"status": "ok", "server": "faxbot-mcp", ...}`. The admin console's **MCP** screen uses them to show whether the built-in server is responding.

Tools

- `send_fax(to, fileContent, fileName, fileType?)`. On stdio, `filePath` or `fileUrl` can replace `fileContent`.
- `get_fax_status(jobId)`
- `get_fax(id)`
- `list_inbound(limit?)`
- `get_inbound_pdf(inboundId, asBase64?)`

See [MCP overview](index.md) for context and tools.
