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

## Sinch Fax API v3 Backend
- "sinch not configured": ensure `FAX_BACKEND=sinch`, `SINCH_PROJECT_ID`, `SINCH_API_KEY`, `SINCH_API_SECRET` (or set `PHAXIO_API_KEY/SECRET` which are used as fallback values).
- Region/base URL: if requests fail, try `SINCH_BASE_URL` (e.g., `https://us.fax.api.sinch.com/v3`).
- Webhooks: current build does not expose a Sinch webhook endpoint; status reflects the immediate response from Sinch. Poll your job via `GET /fax/{id}` if needed.

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

| Transport | Server | Port | Auth | Use Case |
|-----------|--------|------|------|----------|
| **stdio** | `node_mcp/src/servers/stdio.js` or `python_mcp/stdio_server.py` | N/A | `API_KEY` | Desktop AI |
| **Streamable HTTP** | `node_mcp/src/servers/http.js`, `python_mcp/http_server.py`, or built into the API at `/mcp/http/mcp` | 3001 (Node), 3004 (Python) | Each client's own Faxbot key, or OAuth | Web apps, cloud AI |
| **SSE (older clients)** | `python_mcp/server.py`, or built into the API at `/mcp/sse/sse` | 3003 | Each client's own Faxbot key, or OAuth | Clients without Streamable HTTP |

See [MCP Transports](mcp/transports.md) for details.

### Common MCP Problems

#### MCP Usage Tips
- Ensure the main Faxbot API is reachable (`FAX_API_URL`) and your `API_KEY` is set.
- For local files, use tooling that can access your filesystem as needed.

#### Connection & Authentication
- **MCP server not found**: Ensure you’re starting from the correct path:
  - Node servers: `node_mcp/scripts/start-stdio.sh` and `node_mcp/scripts/start-http.sh`
  - Python servers: `python_mcp/` (`stdio_server.py`, `http_server.py`, `server.py`)

### Environment
- `FAX_API_URL` points the MCP server at Faxbot. Only the stdio servers use `API_KEY`; network servers use each client's own key.
- **Authentication failures**: 
  - stdio: Check that `API_KEY` is a valid Faxbot API key with the permissions the tools need
  - Streamable HTTP and SSE: Check that the client sends its own Faxbot key as `Authorization: Bearer <key>` or `X-API-Key`
  - OAuth: Confirm the token has the right `iss` and `aud`, has not expired, and that its subject is listed in `MCP_OAUTH_SUBJECT_KEYS_FILE`
- **Connection refused**: 
  - Ensure main Faxbot API is running on `FAX_API_URL` (default: http://localhost:8080)
  - For network transports, check that the port is free (3001 for Node, 3004 for Python Streamable HTTP, 3003 for Python SSE)
  - Check the server with `GET /health` on its port, or `/mcp/http/health` and `/mcp/sse/health` when built into the API
- **"No tools available"**: MCP server started successfully but tools not loading - check MCP server logs for initialization errors

#### Filesystem Access Required
- **Claude can't read files**: Install and configure filesystem MCP server alongside Faxbot MCP
- **Permission denied**: Check filesystem MCP server has access to directory containing your PDFs
- **Wrong file path**: Use absolute paths or ensure filesystem MCP server is configured for correct directories

### File Types and Paths
- Only PDF and TXT are accepted. Convert images (PNG/JPG) to PDF first.
- macOS: `sips -s format pdf "in.png" --out "out.pdf"`
- Linux: `img2pdf in.png -o out.pdf` or `magick convert in.png out.pdf`
- Filenames with spaces: quote the full path. Example (curl): `-F "file=@'./My File.pdf'"`
- macOS screenshot names sometimes include a “narrow no‑break space” (U+202F) that looks like a regular space and breaks shell quoting. If a quoted path still fails, try renaming with a wildcard: `cp Screenshot*.pdf doc.pdf` and use the new name.

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
