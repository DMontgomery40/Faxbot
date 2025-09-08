---
layout: default
title: MCP Integration
parent: AI Integration
nav_order: 1
permalink: /ai-integration/mcp-integration.html
---

# MCP Integration

Demo
<video src="../assets/images/faxbot_demo.mp4" width="100%" autoplay loop muted playsinline controls>
  <a href="../assets/images/faxbot_demo.mp4">Watch the demo video</a>
  (Your browser or GitHub may not inline-play videos; use the link.)
</video>

What MCP is
- MCP is a protocol. "SSE", "HTTP", and "stdio" are transports. Faxbot provides two server implementations (Node and Python), each capable of running on any of the transports. Pick a language (Node or Python) and a transport (stdio/HTTP/SSE) that fits your environment.

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

Important notes
- File types: only PDF and TXT. Convert images (PNG/JPG) to PDF first.
- Stdio: use `filePath` so the MCP reads the file locally and posts it to Faxbot.
- HTTP/SSE: provide base64 content; MCP JSON limit is ~16 MB; REST API raw limit is 10 MB.
- Backends: works with any Faxbot backend (`phaxio`, `sinch`, or `sip`).

Examples
- "Call send_fax with { to: "+15551234567", filePath: "/Users/me/Documents/letter.pdf" }"
- "Call get_fax_status with { jobId: "<id>" }"

Docker Quick Start (HTTP MCP)
- Start the API and MCP via Docker Compose:
```
docker compose up -d --build api
docker compose --profile mcp up -d --build faxbot-mcp
```
- The MCP HTTP server listens on `http://localhost:3001`.
- Use this when integrating web clients or cloud AI that speak MCP over HTTP.
- For Claude Desktop or Cursor (stdio), run the MCP directly on the host instead of Docker.

Turnkey SSE (HIPAA‑oriented) via Docker Compose
- Node SSE (port 3002):
```
export OAUTH_ISSUER=https://YOUR_ISSUER
export OAUTH_AUDIENCE=faxbot-mcp
export OAUTH_JWKS_URL=https://YOUR_ISSUER/.well-known/jwks.json
docker compose --profile mcp up -d --build faxbot-mcp-sse
```
- Python SSE (port 3003):
```
export OAUTH_ISSUER=https://YOUR_ISSUER
export OAUTH_AUDIENCE=faxbot-mcp
export OAUTH_JWKS_URL=https://YOUR_ISSUER/.well-known/jwks.json
docker compose --profile mcp up -d --build faxbot-mcp-py-sse
```
- Choose one. Both require Bearer JWTs issued by your IdP; tokens are verified via JWKS.
- Detailed OIDC setup guidance and provider links: see OAUTH_SETUP.md.

MCP Inspector (explore tools/resources/prompts)
- Start the Inspector UI via Docker:
```
docker compose --profile mcp up -d mcp-inspector
open http://localhost:6274
```
- Or run locally:
```
npx @modelcontextprotocol/inspector
```
- Connect the Inspector to your Faxbot MCP server:
  - Stdio: launch `node node_mcp/src/servers/stdio.js` (or `python python_mcp/stdio_server.py`) with `FAX_API_URL` and `API_KEY` env.
  - HTTP: set transport "streamable-http" and point to `http://localhost:3001`.
  - SSE: set transport "sse" and point to `http://localhost:3002/sse` (Node) or `http://localhost:3003/sse` (Python). Include `Authorization: Bearer <JWT>`.

Example `mcp.json` for MCP Inspector
```json
{
  "mcpServers": {
    "faxbot-node-stdio": {
      "type": "stdio",
      "command": "node",
      "args": ["src/servers/stdio.js"],
      "cwd": "./node_mcp",
      "env": {
        "FAX_API_URL": "http://localhost:8080",
        "API_KEY": "your_api_key"
      }
    },
    "faxbot-node-http": {
      "type": "streamable-http",
      "url": "http://localhost:3001/mcp"
    },
    "faxbot-node-sse": {
      "type": "sse",
      "url": "http://localhost:3002/sse"
    },
    "faxbot-py-stdio": {
      "type": "stdio",
      "command": "python",
      "args": ["stdio_server.py"],
      "cwd": "./python_mcp",
      "env": {
        "FAX_API_URL": "http://localhost:8080",
        "API_KEY": "your_api_key"
      }
    },
    "faxbot-py-sse": {
      "type": "sse",
      "url": "http://localhost:3003/sse"
    }
  }
}
```
Notes:
- For SSE entries, provide `Authorization: Bearer <JWT>` in the Inspector UI headers before connecting.
- If you keep only one server entry or name one `default-server`, Inspector selects it automatically.

Transports × servers (language matrix)

2 languages × 3 transports = 6 options.

Node MCP:
- stdio: `node_mcp/src/servers/stdio.js`
- HTTP: `node_mcp/src/servers/http.js` (port 3001)
- SSE+OAuth: `node_mcp/src/servers/sse.js` (port 3002)

Python MCP:
- stdio: `python_mcp/stdio_server.py`
- HTTP: `python_mcp/http_server.py`
- SSE+OAuth: `python_mcp/server.py`

 

Node MCP start commands
```
cd node_mcp && npm install
FAX_API_URL=http://localhost:8080 API_KEY=$API_KEY ./scripts/start-stdio.sh     # stdio
FAX_API_URL=http://localhost:8080 API_KEY=$API_KEY MCP_HTTP_PORT=3001 ./scripts/start-http.sh
OAUTH_ISSUER=... OAUTH_AUDIENCE=... FAX_API_URL=http://localhost:8080 API_KEY=$API_KEY \
  MCP_SSE_PORT=3002 ./scripts/start-sse.sh
```

Python MCP start commands
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

HTTP and SSE details
- HTTP uses Streamable HTTP with sessions: POST `/mcp`, GET `/mcp` (SSE), DELETE `/mcp`.
- SSE+OAuth requires Bearer JWT with `iss`/`aud`; JWKS is fetched from the issuer.
- Place HTTP/SSE behind auth/rate limits for production.

Voice examples
- ❌ "Fax document.pdf to +1234567890" (missing file access/base64)
- ✅ "Call send_fax with { to: "+1234567890", filePath: "/path/to/file.pdf" }"
- For HTTP/SSE, read and base64‑encode the file before calling `send_fax`.

File conversion hints
- macOS Preview: File → Export As… → PDF
- macOS CLI: `sips -s format pdf "in.png" --out "out.pdf"`
- Linux: `img2pdf in.png -o out.pdf` or `magick convert in.png out.pdf`
- Windows: "Print to PDF".

See also: Images vs Text PDFs guide (docs/IMAGES_AND_PDFS.md).
