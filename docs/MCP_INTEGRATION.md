# MCP_INTEGRATION.md

## What is MCP?
- Model Context Protocol (2024/2025) lets AI assistants use external tools safely.
- Here, Faxbot exposes two tools: `send_fax` and `get_fax_status`.
- Transports supported: stdio (desktop) and HTTP (server).

## Architecture
Assistant → MCP Server → Faxbot API → Backend (Phaxio or SIP/Asterisk)

## Tools
- send_fax:
  - Input: `{ to, fileContent (base64), fileName, fileType? }` where `fileType` is `pdf` or `txt`.
  - Output: Text with job ID, status.
- get_fax_status:
  - Input: `{ jobId }`
  - Output: Formatted job status.

## Base64 Note
- Current limitation: file content is provided as base64. Large files increase prompt payload. Prefer small PDFs.

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

## Stdio Transport (Claude Desktop, Cursor)
- Start stdio MCP:
```
cd api && npm run start:mcp
```
- Auto-config helper:
```
cd api && node setup-mcp.js
```
- Claude Desktop/Cursor will reference the generated configs to launch `mcp_server.js`.

## HTTP Transport (Cloud/Local)
- Start HTTP MCP:
```
cd api && npm run start:http
```
- Endpoints:
  - GET `/health` – liveness
  - GET `/mcp/capabilities` – tool list
  - POST `/mcp/call` – invoke tool with JSON body
- Example request (send fax):
```
curl -X POST http://localhost:3001/mcp/call \
  -H 'Content-Type: application/json' \
  -d '{
    "name": "send_fax",
    "arguments": {
      "to": "+15551234567",
      "fileName": "note.txt",
      "fileType": "txt",
      "fileContent": "SGVsbG8gV29ybGQh"  
    }
  }'
```

## Security
- If the API uses `X-API-Key`, set `API_KEY` for MCP so it forwards the header.
- For HTTP transport, place behind auth and rate limits.

## Voice Examples
- You can say: “Send this PDF to +1555… using fax tools.” The assistant will call `send_fax` with base64 content, then `get_fax_status`.

