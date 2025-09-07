# Phaxio End-to-End Test (No Physical Fax Required)

Goal: Send a real fax through Phaxio and receive it on a Phaxio number — fully end-to-end, no hardware.

## Prerequisites
- Phaxio account with API key/secret
- A Phaxio receiving number (purchase a temporary number in the Phaxio console)
- An HTTPS URL that Phaxio can reach (for fetching your PDF and posting callbacks)

## 1) Configure Faxbot for Phaxio
Set the following in `.env` at the repo root:

```
FAX_BACKEND=phaxio
API_KEY=<a_secure_key_you_choose>

PHAXIO_API_KEY=<from_phaxio_console>
PHAXIO_API_SECRET=<from_phaxio_console>

# Point these to your public HTTPS URL (set in step 2)
PUBLIC_API_URL=https://<your-public-host>
PHAXIO_CALLBACK_URL=https://<your-public-host>/phaxio-callback
```

Then start the API (cloud-only):
```
make up-cloud
```

## 2) Expose the API via HTTPS
Fast path (auto):
```
./scripts/setup-phaxio-tunnel.sh
```
This will:
- Start a quick tunnel (prefers cloudflared; falls back to ngrok)
- Detect the public HTTPS URL
- Update `.env` (PUBLIC_API_URL, PHAXIO_CALLBACK_URL, FAX_BACKEND=phaxio)
- Restart the API (`make up-cloud`)

Manual alternative:
- Cloudflared: `cloudflared tunnel --url http://localhost:8080`
- ngrok: `ngrok http 8080`
Then copy the HTTPS URL and set `PUBLIC_API_URL` and `PHAXIO_CALLBACK_URL` in `.env`, and restart the API (`make down && make up-cloud`).

## 3) Send a fax to your Phaxio number
Use a simple text file (Faxbot will render it to PDF for you):
```
echo "Test Faxbot → Phaxio end-to-end" > /tmp/fax.txt
FAX_API_URL=http://localhost:8080 API_KEY=$API_KEY \
  ./scripts/send-fax.sh +1YOURPHAXIONUMBER /tmp/fax.txt
```
Sample PDF works too:
```
./scripts/send-fax.sh +1YOURPHAXIONUMBER path/to/sample.pdf
```

The command prints a JSON response with `id`. Use it to poll status:
```
./scripts/get-status.sh <job_id>
```

## 4) Verify Delivery
- Phaxio dashboard (Inbound faxes) should show a received fax.
- Your callback endpoint (`/phaxio-callback`) updates the job status to SUCCESS/FAILED.
- `make logs` tails API logs; you should see `pdf_served` and `job_updated` events.

## Notes
- Production: Use HTTPS for `PUBLIC_API_URL`. Phaxio will fetch your PDF over TLS.
- Security: `API_KEY` protects your `/fax` endpoint; scripts forward it as `X-API-Key`.
- Retry: Phaxio sends webhooks; transient network issues are retried internally.
