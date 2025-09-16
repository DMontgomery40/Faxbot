---
layout: default
title: Webhooks
parent: Backends
nav_order: 10
permalink: /backends/webhooks.html
---

# Webhooks

This page shows provider‑specific webhook endpoints, sample payloads, and signature verification examples.

## Outbound Status — Phaxio

- Endpoint: `POST /phaxio-callback?job_id=<faxbot_job_id>`
- Signature: header `X-Phaxio-Signature` (HMAC‑SHA256 of the raw body using `PHAXIO_API_SECRET`)

Example form payload (Phaxio):
```
fax[id]=123456&fax[status]=success&fax[num_pages]=2
```

Verification (Python):
```python
import hmac, hashlib
secret = PHAXIO_API_SECRET.encode()
digest = hmac.new(secret, raw_body_bytes, hashlib.sha256).hexdigest()
assert hmac.compare_digest(digest, header_value.strip().lower())
```

## Inbound — Phaxio

- Endpoint: `POST /phaxio-inbound`
- Signature: header `X-Phaxio-Signature` (HMAC‑SHA256)

Example JSON payload:
```json
{
  "fax": {
    "id": 98765,
    "from": "+15551230000",
    "to": "+15559870000",
    "num_pages": 3,
    "status": "received",
    "file_url": "https://files.phaxio.com/..."
  }
}
```

Verification (Python): same HMAC pattern as above on the raw request body.

## Inbound — Sinch Fax API v3

- Endpoint: `POST /sinch-inbound`
- Basic auth (optional): set `SINCH_INBOUND_BASIC_USER/PASS`
- HMAC (optional): header `X-Sinch-Signature` with secret `SINCH_INBOUND_HMAC_SECRET`

Example JSON payload (simplified):
```json
{
  "id": "abcd-1234",
  "from": "+15551230000",
  "to": "+15559870000",
  "num_pages": 2,
  "status": "received",
  "file_url": "https://fax.api.sinch.com/v3/..."
}
```

HMAC verification (Python):
```python
import hmac, hashlib
secret = SINCH_INBOUND_HMAC_SECRET.encode()
digest = hmac.new(secret, raw_body_bytes, hashlib.sha256).hexdigest()
assert hmac.compare_digest(digest, header_value.strip().lower())
```

## Inbound — SIP/Asterisk (Self‑Hosted)

- Endpoint: `POST /_internal/asterisk/inbound`
- Header: `X-Internal-Secret: <ASTERISK_INBOUND_SECRET>`
- Body (JSON):
```json
{
  "tiff_path": "/faxdata/in.tiff",
  "to_number": "+15559870000",
  "from_number": "+15551230000",
  "faxstatus": "received",
  "faxpages": 2,
  "uniqueid": "1603261234.89"
}
```

Example curl (internal network):
```
curl -X POST -H 'X-Internal-Secret: <secret>' -H 'Content-Type: application/json' \
  http://api:8080/_internal/asterisk/inbound \
  -d '{"tiff_path":"/faxdata/in.tiff","to_number":"+1555..."}'
```

## Security Tips

- Use HTTPS for all public callbacks.
- Keep secrets out of logs; audit only metadata (job ids, event types).
- Rotate webhook secrets periodically and validate signatures strictly.
