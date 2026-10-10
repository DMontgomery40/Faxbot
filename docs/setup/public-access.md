
# Public Access & Tunnels

Cloud providers must reach your Faxbot API to fetch PDFs and deliver callbacks. When you are experimenting on a laptop, set up a temporary HTTPS tunnel; when you move to production, switch to a real domain behind TLS.

## Quick helpers

- **Cloudflare Tunnel** (`cloudflared`): supplies a public HTTPS URL; quick-tunnel URLs are temporary.
- **ngrok**: fast for demos; remember to lock scopes and rotate URLs frequently

## Manual steps

1. Start your tunnel to `http://localhost:8080`
2. Copy the generated HTTPS URL
3. In **Administration → Setup**, enter the URL in **This server's public address**. You can also set it under **Administration → Security → This server's public address**.
4. Review each provider’s explicit callback URL or its empty-value default derived from Public API URL. Apply with the loaded desired revision and inspect active/pending status. Complete an installation-wide stop/start when pending, then confirm active identity.
5. Inspect Diagnostics configuration checks and perform a controlled provider check separately. A configured URL or local presence check does not prove provider reachability, signed callback receipt or document delivery.

## Production checklist

- Terminate TLS with a certificate you control (ACME, corporate PKI, etc.)
- Enable `ENFORCE_PUBLIC_HTTPS` so Faxbot rejects insecure callback URLs
- Keep provider-specific signing secrets enabled (Phaxio, Sinch, SignalWire)
- Treat tunnel URLs as temporary; move to a proper domain before enabling PHI traffic

For inbound faxes, configure storage under **Administration → Documents & retention**. S3-compatible storage is available, and you can enter an S3 KMS key ID there. Confirm encryption and TLS settings with your storage provider; Faxbot does not ensure that a bucket or endpoint is encrypted.
