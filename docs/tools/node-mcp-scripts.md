# Node MCP Scripts

Helper scripts for running and testing the Node.js MCP server that integrates tools like `send_fax` with assistants.

Prerequisites
- Node.js 20+
- A running Faxbot API (`FAX_API_URL`, default `http://localhost:8080`)
- A Faxbot API key: `API_KEY` for stdio, or each client's own key for Streamable HTTP

Environment
- Scripts inherit `FAX_API_URL` and `API_KEY` from your shell or `.env`. The Streamable HTTP server never uses `API_KEY`; see [Node MCP](../mcp/node.md) for its settings.

Server launchers
- `node_mcp/scripts/start-stdio.sh` — stdio server (`src/servers/stdio.js`)
- `node_mcp/scripts/start-http.sh` — Streamable HTTP server (port 3001)
- The Node package has no SSE server; use `python_mcp/server.py` for SSE-only clients

Tool invocations
- `node node_mcp/scripts/test-stdio.js "+15551234567" /abs/path/sample.pdf`
- `node node_mcp/scripts/call-send-fax.js "+15551234567" /abs/path/sample.pdf`

Notes
- For Streamable HTTP, large files must be base64‑encoded JSON; keep under ~16 MB.
- Prefer stdio + `filePath` for desktop integrations to avoid base64 overhead.
