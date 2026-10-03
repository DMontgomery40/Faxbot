---
layout: default
title: Node MCP Scripts
parent: Scripts and Tests
nav_order: 3
permalink: /scripts-and-tests/node-mcp.html
---

# Node MCP Scripts

Helper scripts for running and testing the Node.js MCP server that integrates tools like `send_fax` with assistants.

Prerequisites
- Node.js 20+
- A running Faxbot API (`FAX_API_URL`, default `http://localhost:8080`)
- A Faxbot API key: `API_KEY` for stdio, or each client's own key for Streamable HTTP

Environment
- Scripts inherit `FAX_API_URL` and `API_KEY` from your shell or `.env`. The Streamable HTTP server also reads `MCP_HTTP_PORT` (default 3001), `MCP_ALLOWED_HOSTS`, `MCP_ALLOWED_ORIGINS` and the OAuth settings in [Node MCP](mcp/node.md); it never uses `API_KEY`.

Server launchers
- `node_mcp/scripts/start-stdio.sh`
  - Starts the stdio transport server (`src/servers/stdio.js`). Best for desktop assistants.
- `node_mcp/scripts/start-http.sh`
  - Starts the Streamable HTTP server on port 3001 (`src/servers/http.js`). Each client sends its own Faxbot key.
- The Node package has no SSE server. For clients that only speak SSE, use the Python server (`python_mcp/server.py`).

Tool invocations
- `node_mcp/scripts/test-stdio.js "<to>" <filePath>`
  - Spawns the stdio server and calls the `send_fax` tool using a local file path (no base64). Example:
    - `node node_mcp/scripts/test-stdio.js "+15551234567" /abs/path/sample.pdf`
- `node_mcp/scripts/call-send-fax.js "<to>" <filePath>`
  - Calls the `send_fax` tool handler directly (bypasses transport) for quick local testing.

Notes
- For Streamable HTTP, large files must be base64‑encoded JSON in requests; keep under the MCP server’s JSON limit (16 MB). The REST API enforces a 10 MB raw file size limit.
- Prefer stdio + `filePath` for desktop integrations to avoid base64 overhead.

