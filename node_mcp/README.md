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

The HTTP+SSE transport is no longer offered: SDK v2 ships it only in a deprecated legacy package. Use Streamable HTTP.

## Per-client keys

Each Streamable HTTP request must carry the caller's own Faxbot API key, as `Authorization: Bearer <key>` or `X-API-Key: <key>`, and the server forwards that key to Faxbot as `X-API-Key`. Requests without a key get 401 and never reach Faxbot. When `OAUTH_ISSUER` and `OAUTH_AUDIENCE` are set, the Bearer token must be a JWT from that issuer, and its `sub` is looked up in the JSON file named by `MCP_OAUTH_SUBJECT_KEYS_FILE` (`{"<subject>": "<faxbot api key>"}`); unmapped subjects get 403. Set `MCP_RESOURCE_URL` to publish OAuth protected-resource metadata. Only the stdio server uses `API_KEY`: every tool call it makes uses that one key.

Other settings: `FAX_API_URL` (default `http://localhost:8080`), `MCP_ALLOWED_HOSTS` (comma-separated Host allowlist; off when unset), `MCP_ALLOWED_ORIGINS` (browser origins allowed to call `/mcp`; none by default).

## Tools

- `send_fax(to, fileContent, fileName, fileType?, operationId?)`. On stdio, `filePath` or `fileUrl` can replace `fileContent`. Returns `{id, status, operationId}`.
- `get_fax_status(jobId)`. Returns the job's `id`, `status`, `to`, `pages`, `error`, `created_at` and `updated_at`.
- `get_fax(id)`: a sent fax job or a received fax.
- `list_inbound(limit?)`. Returns `{items: [...]}`.
- `get_inbound_pdf(inboundId, asBase64?)`. Returns a resource link by default, or the embedded PDF when `asBase64` is true.

Resource template: `faxbot://inbound/{inbound_id}/pdf`, which returns the received fax PDF.

### Sending a fax once

Each `send_fax` call is one fax with an operation id. The server sends the id to Faxbot as the `Idempotency-Key` header and returns it as `operationId`. A call without `operationId` is a new fax, even for a document sent before. The document is read once per call. The call never sends a fax again on its own. When the connection drops, times out, or Faxbot answers 502, 503 or 504, the tool error names the operation id; Faxbot answers a resend with that id with the original job instead of sending the fax twice. A 4xx answer is final.

If no attempt is confirmed, the tool error names the operation id. To finish that same fax, call `send_fax` again with the same number and document plus that id:

```json
{"to": "+15551234567", "fileContent": "<same base64>", "fileName": "letter.pdf", "operationId": "<id from the error>"}
```

The same id with a different number or document fails with HTTP 409. The MCP server keeps no ids or documents; the caller keeps the id. Faxbot servers released before Idempotency-Key support ignore the header, so on those servers resending after a lost response can send the fax twice; check the job list there instead.

## Checks

```
npm run check   # every file parses and every module imports
npm test        # tools, forwarded keys, send-once retries, stdio stdout, ws auth (local fake Faxbot API)
```
