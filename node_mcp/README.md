# Faxbot Node MCP

Node MCP servers for Faxbot, built on the MCP TypeScript SDK v2 (`@modelcontextprotocol/server` 2.3.0, `@modelcontextprotocol/node` 2.1.1). They speak MCP protocol revision 2026-07-28 and still serve clients that use the 2025 `initialize` handshake. Requires Node 20 or newer.

## Install

```
cd node_mcp
npm ci
```

## Transports

| Transport | Start | Endpoint | Faxbot key used for tool calls |
|---|---|---|---|
| stdio | `npm run stdio` or `./scripts/start-stdio.sh` | stdin/stdout | `API_KEY` |
| Streamable HTTP | `npm run http` or `./scripts/start-http.sh` | `POST /mcp` on `MCP_HTTP_PORT` (default 3001), `GET /health` | the calling client's key |
| WebSocket (development bridge, not MCP) | `npm run ws` | `MCP_WS_PORT` (default 3004) | `API_KEY` |

The HTTP+SSE transport is no longer offered: SDK v2 ships it only in a deprecated legacy package. Use Streamable HTTP.

## Per-client keys

Each Streamable HTTP request must carry the caller's own Faxbot API key, as `Authorization: Bearer <key>` or `X-API-Key: <key>`, and the server forwards that key to Faxbot as `X-API-Key`. Requests without a key get 401 and never reach Faxbot. When `OAUTH_ISSUER` and `OAUTH_AUDIENCE` are set, the Bearer token must be a JWT from that issuer, and its `sub` is looked up in the JSON file named by `MCP_OAUTH_SUBJECT_KEYS_FILE` (`{"<subject>": "<faxbot api key>"}`); unmapped subjects get 403. Set `MCP_RESOURCE_URL` to publish OAuth protected-resource metadata. Only stdio and the WebSocket bridge use `API_KEY`, as one integration identity. The WebSocket bridge refuses every connection unless `MCP_WS_API_KEY` (or `API_KEY`) is set, and it accepts that key only in a header.

Other settings: `FAX_API_URL` (default `http://localhost:8080`), `MCP_ALLOWED_HOSTS` (comma-separated Host allowlist; off when unset), `MCP_ALLOWED_ORIGINS` (browser origins allowed to call `/mcp`; none by default).

## Tools

- `send_fax(to, fileContent, fileName, fileType?)`. On stdio, `filePath` or `fileUrl` can replace `fileContent`. Returns `{id, status}`.
- `get_fax_status(jobId)`. Returns the job's `id`, `status`, `to`, `pages`, `error`, `created_at` and `updated_at`.
- `get_fax(id)`: a sent fax job or a received fax.
- `list_inbound(limit?)`. Returns `{items: [...]}`.
- `get_inbound_pdf(inboundId, asBase64?)`. Returns a resource link by default, or the embedded PDF when `asBase64` is true.

Resource template: `faxbot://inbound/{inbound_id}/pdf`, which returns the received fax PDF.

## Checks

```
npm run check   # every file parses and every module imports
npm test        # tools, forwarded keys, stdio stdout, ws auth (local fake Faxbot API)
```
