# Faxbot Node.js SDK

Thin Node.js client for the Faxbot API. Sends faxes and checks status via the unified Faxbot REST API (independent of the server’s backend: Phaxio or SIP/Asterisk).

- Package name: `faxbot`
- Requires: Node.js 18+

`client.plugins` is deprecated and goes away in the next major release; configure providers in the Faxbot console or with the `faxbot` command.

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

## Sending a fax once

Each `sendFax` call is one fax, identified by an operation id that the client sends as the `Idempotency-Key` header. Without `operationId`, the call is a new fax and gets a new id, even for a document you sent before.

If the connection drops, times out, or Faxbot answers 502, 503 or 504, the client sends the same fax again with the same id, up to `retries` more times (default 0, so it never sends again on its own; waiting `retryBackoffMs` and then twice as long each time). Faxbot answers with the original job instead of sending the fax twice. A 4xx answer is never retried.

Every error from `sendFax` carries `operationId`, `status` (the HTTP status, or `null` when no answer arrived) and `uncertain`. When `uncertain` is true, no attempt was confirmed and Faxbot may or may not have the fax. Call `resumeFax` with the error's `operationId` and the same number and document to finish that same fax:

```js
const FaxbotClient = require('faxbot');
const client = new FaxbotClient('http://localhost:8080', 'YOUR_API_KEY', { retries: 2, retryBackoffMs: 500 });

const operationId = FaxbotClient.newOperationId();
await saveSomewhere(operationId); // your own storage; the SDK keeps nothing on disk
try {
  await client.sendFax('+15551234567', 'letter.pdf', { operationId });
} catch (error) {
  if (!error.uncertain) throw error;
  // Now, or after a restart with the saved id:
  await client.resumeFax(error.operationId, '+15551234567', 'letter.pdf');
}
```

- The same id with a different number or document fails with `status` 409.
- The SDK keeps no record of ids or documents. To finish a fax after your program restarts, save the id before sending, as above.
- Faxbot servers released before Idempotency-Key support ignore the header, so a retry after a lost response can send the fax twice. Keep the default `{ retries: 0 }` against those servers and do not resume an unconfirmed send there; check the job list instead.

## Notes
- Only `.pdf` and `.txt` files are accepted.
- If the server requires an API key, it must be supplied via `X-API-Key` (handled automatically when `apiKey` is provided).
- Optional helper: `checkHealth()` pings `/health`.

## Publishing (maintainers)
- Configure GitHub secret `NPM_TOKEN`.
- Create a GitHub Release to trigger publish via CI.

## MCP Note
- MCP (Model Context Protocol) is not part of this SDK. It is a separate integration layer for AI assistants.
- Refer to `docs/mcp/index.md` in the repository for MCP setup and usage.
