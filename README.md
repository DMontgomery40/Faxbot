# Faxbot

Minimal, production-ready, open-source fax-sending API for low traffic. It supports:

- Sending only (no receiving)
- T.38 exclusively (no analog, no G.711 fallback)
- Input formats: PDF and TXT (TXT auto-converted to PDF, then to TIFF for faxing)
- Simple FastAPI service + Asterisk (T.38) via Docker Compose

This is designed for a small MCP/server environment and easy sharing.

## MCP Integration

This fax API includes MCP (Model Context Protocol) support for AI assistant integration.

### Use Cases

- Voice-activated fax sending via Siri/Claude
- Mobile app integration through MCP clients
- Automated document transmission workflows
- Hands-free operation for mobile workers

### MCP Installation

**Automated installer:**
```bash
curl -fsSL https://raw.githubusercontent.com/your-org/faxbot/main/install.sh | bash
```

**Package managers:**
```bash
# npm global install
npm install -g faxbot-mcp

# Manual install
git clone https://github.com/your-org/faxbot
cd faxbot
./install.sh
```

**Start MCP server:**
```bash
# HTTP transport (cloud/mobile compatible)
faxbot-mcp-http

# Stdio transport (desktop apps)
faxbot-mcp
```

**Configuration:**
The installer configures Claude Desktop, Cursor, and system PATH automatically.

### MCP Tools

- **`send_fax`**: Send fax with base64 content, metadata, and priority settings
- **`get_fax_status`**: Get job status with transmission details

### Transport Options

- **HTTP transport**: For cloud deployment and mobile clients
- **Stdio transport**: For desktop applications (Claude, Cursor)
- **Streamable updates**: Real-time progress notifications
- **Enhanced security**: Permission-based access control

### Example Usage

```
User: "Claude, fax my prescription to the pharmacy at 555-0123"
Claude: Uses send_fax tool with document
System: PDF → TIFF → T.38 transmission via Asterisk
Claude: "Fax sent. Job ID: abc123"
```


## Quick Start

1) Copy `.env.example` to `.env` and fill your SIP trunk credentials and identifiers.

```
cp .env.example .env
```

Required values from your SIP provider:
- `SIP_USERNAME`, `SIP_PASSWORD`
- `SIP_SERVER` (e.g., sip.provider.example)
- `SIP_FROM_USER` (e.g., your DID `+15551234567`)
- `SIP_FROM_DOMAIN` (often same domain as server)

2) Launch services:

```
make up
make logs
```

3) Send a fax:

```
curl -X POST http://localhost:8080/fax \
  -H "X-API-Key: $API_KEY" \
  -F to=+15551230001 \
  -F file=@/path/to/document.pdf
```

4) Check status:

```
curl -H "X-API-Key: $API_KEY" http://localhost:8080/fax/<job_id>
```

## Architecture

- `api` (FastAPI):
  - Validates input, enforces size and MIME restrictions (PDF/TXT)
  - Converts TXT -> PDF, then PDF -> TIFF (Ghostscript) for faxing
  - Stores jobs in SQLite
  - Orchestrates fax send by originating a call via Asterisk AMI
  - Listens for `UserEvent(FaxResult, ...)` from Asterisk to update status

- `asterisk`:
  - Configured for T.38 UDPTL
  - Uses PJSIP to register/connect to your SIP trunk
  - Dialplan `faxout` dials destination and runs `SendFAX()` on answer
  - Emits `UserEvent(FaxResult, ...)` with job result

## Endpoints

- `POST /fax` (multipart form):
  - Fields: `to` (E.164 or digits), `file` (PDF/TXT)
  - Returns: Job metadata (id, status)
  - Status starts as `queued` then becomes `in_progress`, `SUCCESS`, or `FAILED`

- `GET /fax/{id}`: Fetch job status.

- `GET /health`: Liveness check

## Configuration

Edit `.env`:

- API/AMI
  - `FAX_DATA_DIR`: Shared volume path for job artifacts
  - `MAX_FILE_SIZE_MB`: Input file size limit
  - `API_KEY`: Required header `X-API-Key` for requests (optional; blank disables auth)
  - `ASTERISK_AMI_*`: AMI connection from API -> Asterisk
  - `FAX_LOCAL_STATION_ID`, `FAX_HEADER`: Sent as identifiers on fax

- SIP (Asterisk)
  - `SIP_USERNAME`, `SIP_PASSWORD`
  - `SIP_SERVER`, `SIP_FROM_USER`, `SIP_FROM_DOMAIN`

Networking/Ports:
- API: `8080`
- SIP: `5060/tcp+udp`
- AMI: `5038`
- UDPTL (T.38): `4000-4999/udp`

## Notes on T.38

- This stack forces T.38 (no analog/G.711 passthrough)
- Ensure your SIP provider supports T.38 UDPTL and permits your public IP
- If behind NAT, adjust port forwards for UDPTL range and 5060

## Testing

Unit tests cover API validation paths (no real fax send). For local testing, set `FAX_DISABLED=true` in `.env` to bypass AMI originate and focus on API behavior.

```
make test
```

## Operational Considerations

- Persistence: SQLite DB file is stored inside the `api` container – for resilience, mount a volume or switch `DATABASE_URL` to Postgres.
- Observability: Asterisk logs are available via `docker compose logs asterisk`. API logs via `docker compose logs api`.
- Security: Restrict API access (private network/VPN, reverse proxy with auth). Validate `to` numbers against allowed patterns/countries.
- File limits: Default 10 MB; adjust per provider capabilities.


## Non-Goals

- Receiving faxes
- High-traffic scaling (no queues/K8s)  
- Multiple document formats beyond PDF/TXT
