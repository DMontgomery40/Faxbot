# Faxbot Node.js SDK

Thin Node.js client for the Faxbot API. Sends faxes and checks status via the unified Faxbot REST API (independent of the server’s backend: Phaxio or SIP/Asterisk).

- Package name: `faxbot`
- Requires: Node.js 18+

## Install

- From npm (once published):
```
npm install faxbot
```
- From source (this repo):
```
cd sdks/node
npm install
```

## Usage
```js
const FaxbotClient = require('faxbot');
const client = new FaxbotClient('http://localhost:8080', 'YOUR_API_KEY');

async function run() {
  const job = await client.sendFax('+15551234567', '/path/to/document.pdf');
  console.log('Queued job:', job.id, job.status);
  const status = await client.getStatus(job.id);
  console.log('Status:', status.status);
}

run().catch(console.error);
```

## Notes
- Only `.pdf` and `.txt` files are accepted.
- If the server requires an API key, it must be supplied via `X-API-Key` (handled automatically when `apiKey` is provided).
- Optional helper: `checkHealth()` pings `/health`.

## Publishing (maintainers)
- Configure GitHub secret `NPM_TOKEN`.
- Create a GitHub Release to trigger publish via CI.

## MCP Note
- MCP (Model Context Protocol) is not part of this SDK. It is a separate integration layer for AI assistants.
- Refer to `docs/MCP_INTEGRATION.md` in the repository for MCP setup and usage.
