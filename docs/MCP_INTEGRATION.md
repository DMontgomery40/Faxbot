# MCP_INTEGRATION.md

## What is MCP?
- Model Context Protocol (2024/2025) lets AI assistants use external tools safely.
- Here, Faxbot exposes two tools: `send_fax` and `get_fax_status`.
- Transports supported: stdio (desktop), HTTP (server), and SSE with OAuth2 (server).

## Transport Options Matrix

Faxbot provides **2 MCP servers × 3 transports = 6 integration options**:

| Transport | File | Port | Auth | Use Case |
|-----------|------|------|------|----------|
| **stdio** | mcp_server.js | N/A | API key | Claude Desktop, Cursor |
| **HTTP** | mcp_http_server.js | 3001 | API key | Web apps, cloud AI |
| **SSE+OAuth** | mcp_sse_server.js | 3002 | JWT/Bearer | Enterprise, HIPAA |

**Quick Selection Guide:**
- **stdio**: Desktop AI assistants (Claude Desktop, Cursor) - simplest setup
- **HTTP**: Web applications, cloud-based AI services - scalable
- **SSE+OAuth**: Enterprise deployments, HIPAA compliance - most secure

## Architecture
Assistant → MCP Server → Faxbot API → Backend (Phaxio or SIP/Asterisk)

## Tools
- send_fax:
  - Input: `{ to, fileContent (base64), fileName, fileType? }` where `fileType` is `pdf` or `txt`.
  - Output: Text with job ID, status.
- get_fax_status:
  - Input: `{ jobId }`
  - Output: Formatted job status.

## ⚠️ Critical Limitation: Base64 File Encoding

**This is a MAJOR user experience limitation that severely constrains real-world usage:**

### What This Means for Users:
- **You CANNOT just say "fax this PDF file"** to Claude and point to a file on your computer
- **The AI assistant must read your file AND convert it to base64 encoding** before calling the fax tools
- **Large PDFs (>1MB) will consume massive amounts of conversation tokens** and may hit model limits
- **This effectively limits faxing to small documents** (few pages max)

### The Technical Problem:
1. MCP protocol requires `fileContent` parameter as base64-encoded string
2. Claude Desktop/AI assistant must:
   - Read the file from your local filesystem (requires filesystem MCP server)
   - Encode entire file as base64 in memory  
   - Pass huge base64 string as tool parameter
   - Base64 encoding increases file size by ~33%

### Realistic User Workflow:
```
❌ NOT POSSIBLE: "Hey Claude, fax document.pdf to +1234567890"

✅ ACTUALLY REQUIRED:
1. User: "Please read document.pdf and fax it to +1234567890" 
2. Claude: Uses filesystem MCP to read file
3. Claude: Converts file to base64 (consuming massive tokens)
4. Claude: Calls send_fax with giant base64 string
5. Faxbot MCP: Decodes base64 back to original file
```

### File Size Impact:
- **Small PDF (100KB)**: ~400KB tokens, usable
- **Typical PDF (1MB)**: ~4MB tokens, may hit limits  
- **Large PDF (5MB)**: ~20MB tokens, **will fail**

### Why This Design Was Chosen:
MCP protocol's JSON-based messaging requires binary data as base64. Alternative approaches (file paths, resource URLs) are emerging in the MCP community but not yet standardized for tool parameters.

## Setup
1) API running at `FAX_API_URL` (default `http://localhost:8080`).
2) Install Node deps in `api/`:
```
cd api && npm install
```
3) Configure environment (optional):
```
export FAX_API_URL=http://localhost:8080
export API_KEY=your_secure_api_key
```

## Quick Start (Scripts)
- macOS/Linux (stdio):
```
FAX_API_URL=http://localhost:8080 API_KEY=$API_KEY \
  ./api/scripts/start-mcp.sh
```

- macOS/Linux (HTTP on port 3001):
```
FAX_API_URL=http://localhost:8080 API_KEY=$API_KEY MCP_HTTP_PORT=3001 \
  ./api/scripts/start-mcp-http.sh
```

- Windows (stdio): double‑click `api\scripts\start-mcp.bat`
- Windows (HTTP): double‑click `api\scripts\start-mcp-http.bat`

Alternative (Makefile shortcuts):
```
make mcp-stdio     # stdio transport
make mcp-http      # HTTP transport (runs in api/)
make mcp-setup     # writes Claude/Cursor configs
```

Global install (adds `faxbot-mcp` and `faxbot-mcp-http` to PATH):
```
cd api && npm run install-global
FAX_API_URL=http://localhost:8080 API_KEY=$API_KEY faxbot-mcp
FAX_API_URL=http://localhost:8080 API_KEY=$API_KEY MCP_HTTP_PORT=3001 faxbot-mcp-http
```

## Stdio Transport (Claude Desktop, Cursor)
- Node (stdio) start:
```
cd api && npm run start:mcp
```
- Auto-config helper:
```
cd api && node setup-mcp.js
```
- Claude Desktop/Cursor will reference the generated configs to launch `mcp_server.js`.

- Python (stdio) start:
```
cd python_mcp
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export FAX_API_URL=http://localhost:8080
export API_KEY=your_api_key
python stdio_server.py
```

## HTTP Transport (Cloud/Local)
- Node (HTTP) start:
```
cd api && npm run start:http
```
- Protocol: Streamable HTTP with session management
  - POST `/mcp` handles JSON-RPC requests. The first request must be an Initialize request if no `Mcp-Session-Id` is provided.
  - GET `/mcp` establishes an SSE stream for server-to-client notifications. Include `Mcp-Session-Id` header.
  - DELETE `/mcp` terminates the session. Include `Mcp-Session-Id` header.
  - CORS: The server exposes `Mcp-Session-Id` and allows `mcp-session-id` header for browser clients.

Note: Streamable HTTP is intended for MCP-aware clients (Claude Desktop, Cursor/Cline, etc.). Manual curl testing requires constructing JSON-RPC requests and handling SSE, which is beyond the scope of this guide.

- Python (HTTP) start:
```
cd python_mcp
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export FAX_API_URL=http://localhost:8080
export API_KEY=your_api_key
uvicorn http_server:app --host 0.0.0.0 --port 3004
```

Docker option (profile `mcp`):
```
make mcp-up     # builds and starts the faxbot-mcp service
make mcp-logs   # tail MCP logs
make mcp-down   # stop MCP service
```

## Security
- If the API uses `X-API-Key`, set `API_KEY` for MCP so it forwards the header.
- For HTTP transport, place behind auth and rate limits.

## SSE Transport with OAuth2 (Node)
- Requirements:
  - Node 18+
  - Dependencies: `npm install jose`
- Configure OAuth2 (resource server):
  - `OAUTH_ISSUER` (e.g., `https://your-tenant.auth0.com`)
  - `OAUTH_AUDIENCE` (e.g., `faxbot-mcp`)
  - `OAUTH_JWKS_URL` (optional override; defaults to `${OAUTH_ISSUER}/.well-known/jwks.json`)
  - `FAX_API_URL` (Faxbot REST API base URL)
  - `API_KEY` (Faxbot API key if enabled)
- Start the server:
```
cd api
npm install  # ensure deps
OAUTH_ISSUER=https://example.auth0.com \
OAUTH_AUDIENCE=faxbot-mcp \
OAUTH_JWKS_URL=https://example.auth0.com/.well-known/jwks.json \
FAX_API_URL=http://localhost:8080 \
API_KEY=your_api_key \
PORT=3002 \
npm run start:sse
```
- Endpoints:
  - `GET /health` → `{ status: 'ok', transport: 'sse', server: 'faxbot-mcp', version: '2.0.0' }`
  - `GET /sse` → starts an SSE session (requires `Authorization: Bearer <JWT>`) and returns events
  - `POST /messages` → send client messages (requires Bearer token and `sessionId`)
  - `DELETE /messages` → close a session
- Notes:
  - JWTs are verified against the JWKS (signature + `iss`/`aud`/`exp`/`nbf`).
  - Sessions are kept in-memory (stateless JWT + stateful transport).

## SSE Transport with OAuth2 (Python)
- Requirements:
  - Python 3.9+
  - Dependencies: see `python_mcp/requirements.txt`
- Configure OAuth2 / Env:
  - `OAUTH_ISSUER`, `OAUTH_AUDIENCE`, `OAUTH_JWKS_URL`
  - `FAX_API_URL`, `API_KEY`
  - `PORT` (default 3003)
- Run:
```
cd python_mcp
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export OAUTH_ISSUER=https://example.auth0.com
export OAUTH_AUDIENCE=faxbot-mcp
export OAUTH_JWKS_URL=https://example.auth0.com/.well-known/jwks.json
export FAX_API_URL=http://localhost:8080
export API_KEY=my_api_key
uvicorn server:app --host 0.0.0.0 --port 3003
```
- Endpoints:
  - `GET /health` → `{ status: 'ok', transport: 'sse', server: 'faxbot-mcp', version: '2.0.0' }`
  - SSE endpoints are mounted at `/` by the MCP SSE app and protected by Bearer auth middleware.
- Notes:
  - The Python MCP server bridges SSE + OAuth2 until official Python MCP SDKs add built-in HTTP/OAuth support.

## Realistic Voice Examples

### ❌ What DOESN'T Work:
```
"Hey Claude, fax document.pdf to +1234567890"
"Send this PDF to the pharmacy"
"Fax my insurance card to Dr. Smith"
```

### ✅ What ACTUALLY Works:
```
"Please read the file insurance-card.pdf from my Documents folder, then fax it to +1234567890"

"Can you open the prescription.pdf file on my desktop and send it via fax to +15551234567?"

"Read the small-report.pdf (make sure it's under 100KB) and fax it to my doctor's office at +19876543210"
```

### Required Setup:
1. **Faxbot MCP server** running (for fax tools)
2. **Filesystem MCP server** running (for file reading)  
3. **Small files only** (under ~100KB for reliable operation)
4. **Explicit file reading request** (Claude must read file first, then fax)

### What Actually Happens:
1. Claude uses filesystem MCP to read your PDF
2. Claude converts file to base64 (consuming massive tokens)
3. Claude calls `send_fax` with base64 content  
4. Claude calls `get_fax_status` to check progress
5. You get confirmation of fax transmission
