# MCP Integration

<div class="grid cards" markdown>

- :material-nodejs: **Node Server**  
  stdio and Streamable HTTP.  
  [Open](node.md)

- :material-language-python: **Python Server**  
  stdio, Streamable HTTP and SSE.  
  [Open](python.md)

- :material-connection: **Transports**  
  Authentication and limits per transport.  
  [Reference](transports.md)

</div>

Faxbot provides MCP servers in Node (MCP TypeScript SDK 2.3) and Python (MCP Python SDK 2.3). Both speak MCP protocol revision 2026-07-28 and still serve clients that use the 2025 `initialize` handshake.

Tools (same names and arguments on every server):

- `send_fax(to, fileContent, fileName, fileType?)`. On stdio, `filePath` or `fileUrl` can replace `fileContent`.
- `get_fax_status(jobId)`
- `get_fax(id)`: a sent fax job or a received fax
- `list_inbound(limit?)`
- `get_inbound_pdf(inboundId, asBase64?)`

Resource template: `faxbot://inbound/{inbound_id}/pdf` (a received fax PDF).

Transports:

- stdio: local desktop assistants (Node and Python)
- Streamable HTTP: the remote transport (Node and Python)
- SSE: compatibility transport for older clients (Python only)

Keys: each Streamable HTTP or SSE request carries the caller's own Faxbot API key (`Authorization: Bearer <key>` or `X-API-Key`), and the server forwards it to Faxbot. With OAuth configured, the token's subject is mapped to a stored Faxbot key. Only stdio uses `API_KEY`, as one integration identity. See [Transports](transports.md).

Limits and file handling

- stdio: use `filePath` to avoid base64 limits
- Streamable HTTP and SSE: base64 `fileContent` only; request bodies up to 16 MB. The REST API's raw file limit is 10 MB
- Allowed types: PDF, TXT

`send_fax` takes an optional `operationId` and returns it. To finish an unconfirmed send, call `send_fax` again with the same number, document and `operationId`; the server treats it as the same fax.
