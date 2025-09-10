---
layout: default
title: Inbound (Receiving Faxes)
nav_order: 4
permalink: /inbound/
---

# Inbound (Receiving Faxes)

> Receive faxes into your Faxbot instance and access them via the Admin Console or API.

## Features

- Webhook‑driven for cloud backends (Phaxio/Sinch Fax) with signature verification
- Asterisk ReceiveFAX pipeline for SIP (TIFF→PDF), mailbox routing
- Short‑lived PDF tokens (TTL configurable), retention windows
- Admin Console inbox with search and filtered views

## API Endpoints

- `GET /inbound` → List recent inbound faxes (JSON)
- `GET /inbound/{id}/pdf` → Download the PDF for a single inbound fax

Both endpoints require API authentication if `API_KEY` is enabled.

## Configuration by Backend

### Phaxio
- Set `PHAXIO_STATUS_CALLBACK_URL` to `https://YOUR_PUBLIC_URL/phaxio-callback`
- Enable signature verification; keep your webhook secret safe
- Ensure your instance enforces HTTPS in production

### Sinch Fax API v3
- Configure delivery webhooks per Sinch docs
- If hosting behind a reverse proxy, forward original scheme/host headers

### SIP / Asterisk (Self‑Hosted)
- Use `ReceiveFAX()` to capture TIFF; convert to PDF on arrival
- Store artifacts locally or S3/MinIO; set retention days
- Optionally route to mailbox(es) based on DID or calling number

## Admin Console

- Inbox tab lists inbound messages; view metadata, open/download PDFs
- Respect retention and token TTL settings from Admin → Settings

## Security Notes

- Enforce HTTPS for public callbacks
- Verify HMAC signatures from cloud providers
- Use scoped API keys; audit admin actions
- Consider KMS for S3 server‑side encryption (if applicable)

