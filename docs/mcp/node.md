# Node MCP

Built on `@modelcontextprotocol/server` 2.3.0 and `@modelcontextprotocol/node` 2.1.1. Requires Node 20 or newer.

Stdio server

- Path: `node_mcp/src/servers/stdio.js`
- Env: `FAX_API_URL`, `API_KEY` (used for every tool call)
- Start: `node node_mcp/src/servers/stdio.js`

Streamable HTTP

- Path: `node_mcp/src/servers/http.js`
- Env: `FAX_API_URL`, `MCP_HTTP_PORT` (default 3001), optional `MCP_ALLOWED_HOSTS`, `MCP_ALLOWED_ORIGINS`
- OAuth (optional): `OAUTH_ISSUER`, `OAUTH_AUDIENCE`, optional `OAUTH_JWKS_URL`, `MCP_OAUTH_SUBJECT_KEYS_FILE`, `MCP_RESOURCE_URL`
- Start: `node node_mcp/src/servers/http.js`
- Endpoints: `POST /mcp`, `GET /health`; with OAuth and `MCP_RESOURCE_URL`, `GET /.well-known/oauth-protected-resource`

WebSocket (development bridge, not an MCP transport)

- Path: `node_mcp/src/servers/ws.js`
- Env: `MCP_WS_PORT` (default 3004), `MCP_WS_API_KEY` or `API_KEY` (required; sent in the `X-API-Key` or `Authorization: Bearer` header)

Tools

- `send_fax(to, fileContent, fileName, fileType?)`. On stdio, `filePath` or `fileUrl` can replace `fileContent`.
- `get_fax_status(jobId)`
- `get_fax(id)`
- `list_inbound(limit?)`
- `get_inbound_pdf(inboundId, asBase64?)`

See [MCP overview](index.md) for context and tools.
