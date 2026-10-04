# SINCH_SETUP.md

Cloud backend using Sinch Fax API v3 ("Phaxio by Sinch"). This backend uploads your PDF directly to Sinch rather than serving a tokenized URL.

When to use
- Prefer this if you have a Sinch account/project and want the v3 direct‑upload flow.
- If you signed up at Phaxio and were redirected to Sinch, your credentials generally work here. You will also need your Sinch Project ID.

Key differences vs `phaxio` backend
- `phaxio`: Provider fetches your PDF via `PUBLIC_API_URL` and posts status to `/phaxio-callback` (HMAC verification supported). No Sinch project ID required.
- `sinch`: Faxbot uploads your PDF directly to Sinch (multipart). PUBLIC_API_URL and `/phaxio-callback` are not used. Outbound status polling uses the accepted job’s captured Sinch account and remote fax ID; inbound webhook ingestion is separate (see Receiving faxes below).

Configuration

On an existing installation, edit the desired Sinch selection and credentials in Settings/Setup, apply with the loaded revision and inspect active/pending status. Complete an installation-wide stop/start when pending. The following environment values are first-bootstrap inputs only.
```env
FAX_BACKEND=sinch
SINCH_PROJECT_ID=your_project_id
SINCH_API_KEY=your_api_key          # falls back to PHAXIO_API_KEY if unset
SINCH_API_SECRET=your_api_secret    # falls back to PHAXIO_API_SECRET if unset
# Optional override region/base URL:
# SINCH_BASE_URL=https://fax.api.sinch.com/v3

# General
API_KEY=your_secure_api_key         # optional but recommended (X-API-Key)
MAX_FILE_SIZE_MB=10
```

Send a fax (curl)
```bash
curl -X POST http://localhost:8080/fax \
  -H "X-API-Key: $API_KEY" \
  -F to=+15551234567 \
  -F file=@./example.pdf
```
The 202 response includes a durable job ID and delivery state. Acceptance precedes provider submission; disabled sending leaves the job held even after sending is re-enabled.

### Quick examples (SDKs)

=== "Node"

```js
const FaxbotClient = require('faxbot');
const client = new FaxbotClient('http://localhost:8080', process.env.API_KEY);
(async () => {
  const job = await client.sendFax('+15551234567', './example.pdf');
  console.log('Queued:', job.id);
})();
```

=== "Python"

```python
from faxbot import FaxbotClient
client = FaxbotClient('http://localhost:8080', api_key=os.getenv('API_KEY'))
job = client.send_fax('+15551234567', './example.pdf')
print('Queued', job['id'])
```

Status updates
- Read `GET /fax/{id}` or Jobs for stored delivery state. Supported status polling uses the original captured account/remote ID and does not resubmit. An unknown outcome requires reconciliation with that account, not a blind retry.

Notes
- Only PDF and TXT files are accepted. Convert images (PNG/JPG) to PDF first.
- Avoid exposing credentials. Place Faxbot behind HTTPS and a reverse proxy with rate limiting.

Troubleshooting
- 401: invalid API key to your Faxbot API (set `API_KEY` and send `X-API-Key`).
- 413: file too large → raise `MAX_FILE_SIZE_MB`.
- 415: unsupported file type → only PDF/TXT.
- Sinch API errors: verify Project ID, API key/secret, and region.

## Receiving faxes

Set the incoming fax webhook of your Sinch fax service to your public address followed by `/sinch-inbound`. Protect it with basic auth (`SINCH_INBOUND_BASIC_USER` and `SINCH_INBOUND_BASIC_PASS`). Faxbot then accepts the document Sinch attaches. Without basic auth, Faxbot confirms each fax in your Sinch project before recording it, and downloads the document from Sinch's API. See [Receiving faxes](../operations/receiving.md).
