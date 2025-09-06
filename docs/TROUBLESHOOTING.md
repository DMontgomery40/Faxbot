# TROUBLESHOOTING.md

## General
- 401 Invalid API key: set `API_KEY` and pass `X-API-Key` header.
- 413 File too large: adjust `MAX_FILE_SIZE_MB`.
- 415 Unsupported file type: only PDF and TXT allowed.
- Prefer HTTPS for `PUBLIC_API_URL` in production. The cloud backend fetches PDFs from your server; use TLS.

## Phaxio Backend
- "phaxio not configured": ensure `FAX_BACKEND=phaxio`, `PHAXIO_API_KEY`, `PHAXIO_API_SECRET`.
- No status updates: verify your callback URL (`PHAXIO_CALLBACK_URL` or `PHAXIO_STATUS_CALLBACK_URL`) and that your server is publicly reachable.
- 403 on `/fax/{id}/pdf`: invalid token or wrong `PUBLIC_API_URL`.
- Phaxio API error: confirm credentials and sufficient account balance.

## SIP/Asterisk Backend
- AMI connection failed:
  - Asterisk container running and reachable on `5038`.
  - `ASTERISK_AMI_*` match `manager.conf` template.
  - API logs show reconnect with exponential backoff.
- T.38 negotiation failed:
  - Provider supports UDPTL.
  - Firewall forwards UDP `4000-4999`.
  - `pjsip.conf` has `t38_udptl=yes` and redundancy.
- No fax send:
  - Check Asterisk logs for `SendFAX` and `FaxResult` events.
  - Validate destination formatting.

## Conversion
- Ghostscript missing: API warns and stubs TIFF conversion; install `ghostscript` for production.
- Garbled pages: use clean PDF fonts or provide PDF instead of TXT.

## MCP (AI Assistant Integration)

### Transport Selection Issues
If you're unsure which MCP transport to use:

| Transport | File | Port | Auth | Use Case |
|-----------|------|------|------|----------|
| **stdio** | api/mcp_server.js or node_mcp/src/servers/stdio.js | N/A | API key | Desktop AI |
| **HTTP** | api/mcp_http_server.js or node_mcp/src/servers/http.js | 3001 | API key | Web apps, cloud AI |
| **SSE+OAuth** | api/mcp_sse_server.js or node_mcp/src/servers/sse.js | 3002 | JWT/Bearer | Enterprise, HIPAA |

### Common MCP Problems

#### Base64 File Handling (MAJOR LIMITATION)
- **"File too large" or token limit errors**: PDFs >1MB will likely fail due to base64 token consumption
- **AI assistant can't find file**: You need BOTH faxbot MCP AND filesystem MCP servers running
- **Workflow confusion**: You can't just say "fax this file" - AI must read file first, then encode as base64
- **Real size limits**: 
  - 100KB PDF = usable
  - 500KB PDF = borderline  
  - 1MB+ PDF = probably fails
- **Recommended Workaround (OCR)**: Use the Faxbot OCR workflow to extract text locally and send as TXT fax.
  - Node prompt: `faxbot_pdf` (in `node_mcp/`)
  - Python tool: `faxbot_pdf(pdf_path, to, header_text?)` (in `python_mcp/`)
  - Start (Node): `cd node_mcp && npm install && ./scripts/start-stdio.sh`
  - Start (Python): `cd python_mcp && python -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt && python stdio_server.py`
  - Env: set `FAX_API_URL` and `API_KEY`; optional `MAX_TEXT_SIZE`, `FAXBOT_OCR_ENABLE=true`, `FAXBOT_OCR_DPI=200`

#### Connection & Authentication
- **MCP server not found**: Ensure you’re starting from the correct path:
  - Legacy servers: `api/scripts/start-mcp*.sh`
  - New servers (recommended): `node_mcp/scripts/start-*.sh`
  - Python servers: `python_mcp/` (`stdio_server.py`, `http_server.py`, `server.py`)

### OCR Issues (Python)
- `pytesseract` errors: Install Tesseract OCR (macOS: `brew install tesseract`, Ubuntu: `sudo apt-get install tesseract-ocr`). Ensure `tesseract` is on PATH or set `TESSERACT_CMD`.
- `pypdfium2` missing: Run `pip install -r python_mcp/requirements.txt` within a virtualenv.
- OCR not triggered: Ensure `FAXBOT_OCR_ENABLE=true`. The code only falls back to OCR when extracted text is empty or very short.
- **Authentication failures**: 
  - stdio: Check `API_KEY` environment variable matches Faxbot API setting
  - HTTP: Verify `X-API-Key` header is being passed correctly
  - SSE+OAuth: Confirm JWT token has correct `iss`, `aud`, and hasn't expired
- **Connection refused**: 
  - Ensure main Faxbot API is running on `FAX_API_URL` (default: http://localhost:8080)
  - For HTTP/SSE transports, check port availability (3001/3002)
- **"No tools available"**: MCP server started successfully but tools not loading - check MCP server logs for initialization errors

#### Filesystem Access Required
- **Claude can't read files**: Install and configure filesystem MCP server alongside Faxbot MCP
- **Permission denied**: Check filesystem MCP server has access to directory containing your PDFs
- **Wrong file path**: Use absolute paths or ensure filesystem MCP server is configured for correct directories

## Reverse Proxy Examples (Rate Limiting)

Nginx (basic example):
```
server {
  listen 443 ssl;
  server_name your-domain.com;

  # ... ssl_certificate / ssl_certificate_key ...

  # Simple rate limit per IP
  limit_req_zone $binary_remote_addr zone=faxbot:10m rate=5r/s;

  location / {
    limit_req zone=faxbot burst=10 nodelay;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header Host $host;
    proxy_pass http://127.0.0.1:8080;
  }
}
```

Caddy (basic example):
```
your-domain.com {
  reverse_proxy 127.0.0.1:8080
  # Rate limiting plugins vary; consider layer-4 or WAF if needed
}
```
