# Client SDKs

Thin, official clients for the Faxbot API. They call the unified Faxbot REST API (no direct Phaxio/Asterisk calls). Current version alignment: Python 1.0.2, Node 1.0.2.

- Python: `faxbot`
- Node.js: `faxbot`

## Python
- Install:
```
pip install faxbot
```
- Usage:
```python
from faxbot import FaxbotClient

client = FaxbotClient(base_url="http://localhost:8080", api_key="YOUR_API_KEY")
job = client.send_fax("+15551234567", "/path/to/document.pdf")
print("Queued:", job["id"], job["status"])
status = client.get_status(job["id"])
print("Status:", status["status"]) 
```
- Notes:
  - Only `.pdf` and `.txt` files are accepted.
  - If `API_KEY` is enabled on the server, the client sends it via `X-API-Key`.
  - Optional: `check_health()` calls `/health`.

## Node.js
- Install:
```
npm install faxbot
```
- Usage:
```js
const FaxbotClient = require('faxbot');
const client = new FaxbotClient('http://localhost:8080', 'YOUR_API_KEY');

(async () => {
  const job = await client.sendFax('+15551234567', '/path/to/document.pdf');
  console.log('Queued:', job.id, job.status);
  const status = await client.getStatus(job.id);
  console.log('Status:', status.status);
})();
```
- Notes:
  - Only `.pdf` and `.txt` files are accepted.
  - If `API_KEY` is enabled, `X-API-Key` header is added automatically.
  - Optional: `checkHealth()` calls `/health`.

## Errors
- The SDKs raise/throw on non-2xx responses. Common cases:
  - 400: invalid phone number or parameters
  - 401: missing/invalid API key
  - 413: file too large
  - 415: unsupported media type (non-PDF/TXT)
  - 404: job not found (for GET /fax/{id})

## Compatibility
- The SDKs work regardless of backend (`phaxio`, `sinch`, or `sip`) because Faxbot abstracts the difference.

## MCP vs SDK
- The SDKs do not include MCP (Model Context Protocol) logic. They are simple HTTP clients for developers.
- MCP integration is a separate component (stdio/HTTP servers) for AI assistants.
- See the guide: docs/MCP_INTEGRATION.md for setup, transports, and examples.
