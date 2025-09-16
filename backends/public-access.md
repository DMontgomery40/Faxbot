---
layout: default
title: Public Access & Tunnels
parent: Backends
nav_order: 9
permalink: /backends/public-access.html
---

# Public Access & Tunnels

Cloud providers must reach your Faxbot API to fetch PDFs and deliver callbacks. When you are experimenting on a laptop, set up a temporary HTTPS tunnel; when you move to production, switch to a real domain behind TLS.

## Quick helpers

- **Cloudflare Tunnel** (`cloudflared`): free, persistent, HIPAA-friendly when terminated locally
- **ngrok**: fast for demos; remember to lock scopes and rotate URLs frequently
- Scripted helper: `scripts/setup-phaxio-tunnel.sh` spins up a tunnel, sets `PUBLIC_API_URL`, updates callback URLs, and restarts the API container if Docker is available

## Manual steps

1. Start your tunnel to `http://localhost:8080`
2. Copy the generated HTTPS URL
3. In the Admin Console Setup Wizard (Phaxio/SignalWire) or **Settings → Backends**, paste the URL when prompted
4. Faxbot will derive the callback endpoint (`<public>/phaxio-callback`, `<public>/signalwire-callback`, etc.) automatically
5. Use **Diagnostics → Webhooks** to verify reachability and signature status

## Production checklist

- Terminate TLS with a certificate you control (ACME, corporate PKI, etc.)
- Enable `ENFORCE_PUBLIC_HTTPS` so Faxbot rejects insecure callback URLs
- Keep provider-specific signing secrets enabled (Phaxio, Sinch, SignalWire)
- Treat tunnel URLs as temporary; move to a proper domain before enabling PHI traffic

Need inbound receiving? Pair this guide with **Settings → Storage** to ensure PDFs are stored in encrypted buckets (S3/SSE-KMS or MinIO with TLS).
