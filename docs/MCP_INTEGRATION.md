# MCP_INTEGRATION.md

Quick Start (Claude/Cursor)
- Add Faxbot MCP to your assistant config (stdio). Then call send_fax with a local filePath.

Claude Desktop or Cursor config example:
```
{
  "mcpServers": {
    "faxbot": {
      "command": "node",
      "args": ["src/servers/stdio.js"],
      "cwd": "/PATH/TO/faxbot/node_mcp",
      "env": { "FAX_API_URL": "http://localhost:8080", "API_KEY": "your_api_key" }
    }
  }
}
```

Use these tools
- send_fax: `{ to, filePath }` (stdio) or `{ to, fileContent, fileName, fileType }` (HTTP/SSE)
- get_fax_status: `{ jobId }`
- faxbot_pdf (stdio convenience): `{ pdf_path, to, header_text? }` — for text‑based PDFs only; do not use for scanned/image PDFs.

Important notes
- File types: only PDF and TXT. Convert images (PNG/JPG) to PDF first.
- Stdio: use `filePath` so the MCP reads the file locally and posts it to Faxbot.
- HTTP/SSE: provide base64 content and keep files small (≤ ~100 KB).
- Backends: works with any Faxbot backend (`phaxio`, `sinch`, or `sip`).
 - Scanned/image PDFs: use `send_fax` with `filePath` to send the original image PDF; do not call `faxbot_pdf`.

Examples
- “Call send_fax with { to: "+15551234567", filePath: "/Users/me/Documents/letter.pdf" }”
- “Call get_fax_status with { jobId: "<id>" }”

Details
<details>
<summary>Transports and servers</summary>

2 MCP servers × 3 transports = 6 options.

Legacy /api servers (Node): `mcp_server.js`, `mcp_http_server.js`, `mcp_sse_server.js`

Node MCP (recommended):
- stdio: `node_mcp/src/servers/stdio.js`
- HTTP: `node_mcp/src/servers/http.js` (port 3001)
- SSE+OAuth: `node_mcp/src/servers/sse.js` (port 3002)

Python MCP:
- stdio: `python_mcp/stdio_server.py`
- HTTP: `python_mcp/http_server.py`
- SSE+OAuth: `python_mcp/server.py`

</details>

<details>
<summary>Node MCP start commands</summary>

```
cd node_mcp && npm install
FAX_API_URL=http://localhost:8080 API_KEY=$API_KEY ./scripts/start-stdio.sh     # stdio
FAX_API_URL=http://localhost:8080 API_KEY=$API_KEY MCP_HTTP_PORT=3001 ./scripts/start-http.sh
OAUTH_ISSUER=... OAUTH_AUDIENCE=... FAX_API_URL=http://localhost:8080 API_KEY=$API_KEY \
  MCP_SSE_PORT=3002 ./scripts/start-sse.sh
```

</details>

<details>
<summary>Python MCP start commands</summary>

```
cd python_mcp
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export FAX_API_URL=http://localhost:8080
export API_KEY=your_api_key
python stdio_server.py               # stdio
# or: uvicorn http_server:app --host 0.0.0.0 --port 3004
# or: uvicorn server:app --host 0.0.0.0 --port 3003 (SSE+OAuth)
```

OCR (optional): set `FAXBOT_OCR_ENABLE=true` and install Tesseract; see `python_mcp/text_extract.py`.

</details>

<details>
<summary>HTTP and SSE details</summary>

- HTTP uses Streamable HTTP with sessions: POST `/mcp`, GET `/mcp` (SSE), DELETE `/mcp`.
- SSE+OAuth requires Bearer JWT with `iss`/`aud`; JWKS is fetched from the issuer.
- Place HTTP/SSE behind auth/rate limits for production.

</details>

<details>
<summary>Legacy /api servers</summary>

Scripts and Make targets exist under `api/`. These servers are base64‑only for `send_fax`.

</details>

<details>
<summary>Voice examples</summary>

❌ “Fax document.pdf to +1234567890” (missing file access/base64)

✅ “Call send_fax with { to: "+1234567890", filePath: "/path/to/file.pdf" }”

For HTTP/SSE, read and base64‑encode the file before calling `send_fax`.

</details>

<details>
<summary>File conversion hints</summary>

- macOS Preview: File → Export As… → PDF
- macOS CLI: `sips -s format pdf "in.png" --out "out.pdf"`
- Linux: `img2pdf in.png -o out.pdf` or `magick convert in.png out.pdf`
- Windows: “Print to PDF”.

</details>

See also: Images vs Text PDFs guide (docs/IMAGES_AND_PDFS.md).
