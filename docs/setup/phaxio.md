# Phaxio setup

## Overview

- Cloud backend for sending faxes via Phaxio (also branded “Phaxio by Sinch”).
- Easiest option; no SIP or telephony expertise required.
- Sends faxes, and can receive them when inbound is enabled with Phaxio; see [Receiving faxes](#receiving-faxes).

## Prerequisites

- Phaxio account and API credentials.
- Public URL for callbacks and PDF access (domain or tunnel like ngrok).
- Docker and Docker Compose installed.

## Steps

### 1. Create Phaxio account and get credentials

- Log in to the Phaxio console and retrieve:
    - PHAXIO_API_KEY
    - PHAXIO_API_SECRET
    - PHAXIO_CALLBACK_TOKEN: the separate account Callback Token, required for authenticated outbound status callbacks.

Note on branding: New Phaxio signups and dashboards may redirect to Sinch. That is expected — Phaxio is a Sinch company. This backend continues to work with those credentials.

### 2. Configure Faxbot

On an existing installation, open Settings or Setup, select Phaxio for outbound and fill in the credential and address fields. Apply only the changes you mean. If Faxbot asks for a restart, stop every API process and start the installation again. An empty outbound or inbound override uses the default provider.

The following `.env` values only apply when a new installation starts for the first time; later edits are not imported:

```env
FAX_BACKEND=phaxio
PHAXIO_API_KEY=your_key
PHAXIO_API_SECRET=your_secret
PHAXIO_CALLBACK_TOKEN=your_account_callback_token
PHAXIO_VERIFY_SIGNATURE=true
PUBLIC_API_URL=https://your-domain.com
PHAXIO_STATUS_CALLBACK_URL=https://your-domain.com/phaxio-callback
# PHAXIO_CALLBACK_URL is also accepted at bootstrap
API_KEY=your_secure_api_key   # Optional but recommended; used as X-API-Key
```

- Note: PUBLIC_API_URL must be reachable by Phaxio to fetch PDFs.
- For production, set `ENFORCE_PUBLIC_HTTPS=true` to require HTTPS (recommended). For local testing, leave it false.

Sinch v3 vs legacy Phaxio: If you prefer Sinch’s Fax API v3 “direct upload” flow, use the separate `sinch` backend instead (see SINCH_SETUP.md). This guide covers the classic Phaxio-style flow where the provider fetches your PDF via a tokenized URL and posts status to `/phaxio-callback`.

### 3. Start the API

```bash
make up-cloud   # or: docker compose up -d --build api
```

- API will listen on `http://localhost:8080` by default.

How this works: you talk to the Faxbot API (your local/server endpoint). Faxbot then calls the official Phaxio API on your behalf and gives Phaxio a public URL to fetch your PDF. You do not call Phaxio endpoints directly from your client. Ensure `PUBLIC_API_URL` is reachable from Phaxio and that your callback URL (`PHAXIO_CALLBACK_URL` or `PHAXIO_STATUS_CALLBACK_URL`) points back to your server.

### 4. Test sending a fax

- PDF/TXT preparation preserves document contents. Phaxio receives a PDF URL; its builtin path does not require TIFF conversion.
- Example (replace number):

    ```bash
    curl -X POST http://localhost:8080/fax \
      -H "X-API-Key: your_secure_api_key" \
      -F to=+15551234567 \
      -F file=@./example.pdf
    ```

- The 202 response includes the job ID and delivery details. It means Faxbot accepted the fax, not that it was delivered. While sending is disabled, faxes are held and are never sent automatically later. Check Jobs and your Phaxio account for the real result.
- Check status:

    ```bash
    curl -H "X-API-Key: your_secure_api_key" http://localhost:8080/fax/<job_id>
    ```

#### Quick examples (SDKs)

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

### 5. Configure callback (optional but recommended)

- Phaxio will POST status to your callback URL (`PHAXIO_CALLBACK_URL` or `PHAXIO_STATUS_CALLBACK_URL`).
- Faxbot adds `job_id` and `attempt_id` query locators to the URL submitted to Phaxio. A locator alone does not authenticate a callback: the signature, captured provider account, attempt and provider fax ID must also match.
- Ensure your PUBLIC_API_URL and callback URL are reachable from Phaxio.
- Obtain the account Callback Token from Phaxio's callback settings and set `PHAXIO_CALLBACK_TOKEN`; `PHAXIO_API_SECRET` remains the send/status API credential. Faxbot verifies `X-Phaxio-Signature` as lowercase hexadecimal HMAC-SHA1 over the exact captured callback URL/query, stably name-sorted form fields and file-part SHA1 digests. See [the outbound callback contract](webhooks.md#outbound-status-phaxio).
- A job captured with `PHAXIO_VERIFY_SIGNATURE=false` disables outbound callback updates by rejecting them; it does not accept unsigned updates. Status polling still uses its captured original API credentials when its provider fax ID is known. Later configuration changes do not replace a previously accepted job's captured callback token, URL or verification setting.
- Optional retention: set `ARTIFACT_TTL_DAYS>0` to automatically delete PDFs after the specified number of days (cleanup runs daily by default).

## Costs & HIPAA

- Phaxio pricing: see their site for per-page costs.
- HIPAA information (BAA): https://www.phaxio.com/docs/security/hipaa

## Security Notes

- Set a strong `API_KEY` and send it as `X-API-Key`.
- Rate limit and restrict access via reverse proxy (nginx, Caddy, etc.).
- The PDF serving endpoint uses a tokenized URL; treat PUBLIC_API_URL as sensitive.
- Use HTTPS for `PUBLIC_API_URL` in production so Phaxio fetches over TLS. HTTP is fine for local development only.

## Number Format

- Use E.164 format (e.g., `+15551234567`) for best results.
- The backend performs limited normalization for non‑E.164 input, but E.164 avoids ambiguity across regions.

## Troubleshooting

- "phaxio not configured": verify `FAX_BACKEND=phaxio` and both API key/secret.
- No callback updates: confirm the captured callback URL/query, public reachability, account Callback Token and `PHAXIO_VERIFY_SIGNATURE=true`. Polling can still report status from the captured original account.
- 403 when fetching PDF: token mismatch or expired URL.
- See docs/TROUBLESHOOTING.md for more.

## Related: Sinch Fax API v3

Phaxio is part of Sinch. If your console shows Sinch and you prefer the v3 API’s direct upload model (and features like their own webhooks), use the `sinch` backend. See SINCH_SETUP.md. Your existing Phaxio credentials typically work as Sinch API credentials; you will also need the Sinch Project ID.

## Receiving faxes

1. In Settings, turn Inbound on and choose Phaxio for receiving.
2. Enter the Callback Token (`PHAXIO_CALLBACK_TOKEN`) along with the API key and secret.
3. In the Phaxio console, set the receive callback URL to your public address followed by `/phaxio-inbound`.

Faxbot checks Phaxio's signature on every notification. It downloads the document from Phaxio's API when the notification does not carry it. A fax shows as **Waiting for the document** until the document has arrived. See [Receiving faxes](../operations/receiving.md) for the statuses, **Fetch again** and what happens when signature checks are off.
