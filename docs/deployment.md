
# Deployment

Services
- `api`: FastAPI service (required)
- `asterisk`: SIP/Asterisk backend (only when SIP is selected for inbound or outbound)
- `faxbot-mcp`: MCP server (optional)

Ports
- `8080`: API
- `3001`: MCP Streamable HTTP (Node)
- `3004`: MCP Streamable HTTP (Python)
- `3003`: MCP SSE (Python, for older clients)
- SIP/Asterisk only: `5060` (SIP), `5038` (AMI internal), `4000-4999` (UDPTL)

Storage and database
- `FAX_DATA_DIR` for PDFs/TIFFs (default `./faxdata`)
- SQLite for dev; use Postgres in production (`DATABASE_URL`)
- S3/S3‑compatible for inbound artifacts; for SSE‑KMS see AWS docs below

Configuration and activation

- Environment variables and the legacy plugin JSON file are read once, when a new installation starts for the first time. Keep container ports, mounts and telephony service settings in the deployment configuration.
- On an existing installation, change settings on the admin console's Settings screen and apply.
- When Settings asks for a restart, stop every API process and start the installation again, then load Settings to confirm nothing is pending. Restarting one process while others keep running, or using the reload button, is not enough.
- Back up the database, the installation key file and stored documents together; the exported `.env` template is not a backup.

Public URL and TLS

- In Settings, set `PUBLIC_API_URL` to your HTTPS endpoint
- `ENFORCE_PUBLIC_HTTPS=true` for production with cloud backends
- For quick testing, use a tunnel:
  - Cloudflare Tunnel: <https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/do-more-with-tunnels/trycloudflare/>
  - ngrok (HTTP): <https://ngrok.com/docs/guides/http/>

Artifacts cleanup
- `ARTIFACT_TTL_DAYS` to delete old PDFs/TIFFs after completion
- `CLEANUP_INTERVAL_MINUTES` controls sweep frequency

Security
- Set `API_KEY` and require `X-API-Key` header
- Never expose AMI (5038) publicly
- Use HTTPS for callbacks and public endpoints

References
- AWS S3 SSE‑KMS: <https://docs.aws.amazon.com/AmazonS3/latest/userguide/UsingKMSEncryption.html>
- Third‑Party docs: [Third-Party](third-party.md)
