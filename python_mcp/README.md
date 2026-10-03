# Faxbot Python MCP runtime

Requires Python 3.11 or newer. Built on the MCP Python SDK 2.3.0 (`mcp`, `MCPServer`), which speaks MCP protocol revision 2026-07-28 and still serves clients that use the 2025 `initialize` handshake. Tools: `send_fax`, `get_fax_status`, `get_fax`, `list_inbound`, `get_inbound_pdf`; resource template `faxbot://inbound/{inbound_id}/pdf`.

From the repository root, install the API and MCP requirements in one resolver transaction:

```sh
python -m venv /tmp/faxbot-runtime
/tmp/faxbot-runtime/bin/python -m pip install -r api/requirements.txt -r python_mcp/requirements.txt
/tmp/faxbot-runtime/bin/python -m pip check
```

For a standalone MCP environment, use `pip install -r python_mcp/requirements.txt`, or install the package with `pip install -c python_mcp/runtime-constraints.txt ./python_mcp`. The package exposes `faxbot-mcp-py-stdio`, `faxbot-mcp-py-http` (Streamable HTTP, port 3004) and `faxbot-mcp-py-sse` (SSE compatibility transport, port 3003); `PORT` overrides the HTTP/SSE port. Set `FAX_API_URL` for the backend.

Keys: stdio uses `API_KEY` for every tool call. Streamable HTTP and SSE never use `API_KEY`: each request must carry the caller's own Faxbot key (`Authorization: Bearer <key>` or `X-API-Key`), which is forwarded to Faxbot. With OAuth (`OAUTH_ISSUER`, `OAUTH_AUDIENCE`, optional `OAUTH_JWKS_URL`), the token's `sub` is mapped to a key from the JSON file named by `MCP_OAUTH_SUBJECT_KEYS_FILE`; `MCP_RESOURCE_URL` publishes protected-resource metadata. `MCP_ALLOWED_HOSTS` enables a Host allowlist; browser origins are refused unless listed in `MCP_ALLOWED_ORIGINS`.

Sending a fax once: each `send_fax` call is one fax with an operation id, sent to Faxbot as the `Idempotency-Key` header and returned as `operationId` with `id` and `status`. A call without `operationId` is a new fax, even for a document sent before. When the connection drops, times out, or Faxbot answers 502, 503 or 504, the call sends the same request again with the same id (up to 2 more times, waiting 0.5 s and then 1 s); Faxbot answers with the original job instead of sending the fax twice. A 4xx answer is never retried. If no attempt is confirmed, the tool error names the operation id. To finish that same fax, call `send_fax` again with the same number and document plus that id:

```json
{"to": "+15551234567", "fileContent": "<same base64>", "fileName": "letter.pdf", "operationId": "<id from the error>"}
```

The same id with a different number or document fails with HTTP 409. The MCP server keeps no ids or documents; the caller keeps the id. Faxbot servers released before Idempotency-Key support ignore the header, so on those servers a retry after a lost response can send the fax twice.

Embedded API transports are enabled with `ENABLE_MCP_HTTP=true` or `ENABLE_MCP_SSE=true`. With default prefixes, protocol URLs are `/mcp/http/mcp` and `/mcp/sse/sse`; prefix overrides retain the SDK endpoint suffixes. Enabled transports are required startup dependencies. The API explicitly owns their lifespans.

Tests: `python -m pytest -q python_mcp/tests` (needs `pytest`, `pytest-asyncio` and `cryptography`).

`runtime-constraints.txt` pins the combined Python dependency resolution. Constraints do not install packages: platform/extra dependencies still follow package metadata. To update it, choose compatible stable direct versions from official package metadata, update both requirement declarations and project metadata together, then resolve both requirements in one transaction in **fresh Python 3.11 Linux and macOS environments**. During regeneration omit the existing constraints with temporary copies of the requirements; freeze the union of both resolutions into the constraint file (omit pip/setuptools/wheel). Retain environment markers for any platform-exclusive packages. Check both environments with `pip check`, run the complete API/runtime suite, build `api/Dockerfile`, and verify actual HTTP and MCP handshakes before accepting the update. Do not freeze an unrelated developer environment.
