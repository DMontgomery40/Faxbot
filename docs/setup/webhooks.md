
# Webhooks

This page shows provider‑specific webhook endpoints, sample payloads, and signature verification examples.

## Outbound Status — Phaxio

- Endpoint: `POST /phaxio-callback?job_id=<faxbot_job_id>&attempt_id=<attempt_id>`.
- Signature: `X-Phaxio-Signature`, lowercase hexadecimal HMAC-SHA1 using the separate account `PHAXIO_CALLBACK_TOKEN`.

Faxbot captures the callback override, or the accepted public API URL plus `/phaxio-callback`, with the job's original provider profile. It preserves unrelated query bytes/order and adds the job and attempt locators before submitting the URL. Verification uses that exact public URL, including its query and trailing slash; it does not reconstruct a URL from request or proxy headers. Both locators, the captured provider account and the provider fax ID must match. Query IDs alone grant no authority to update a job.

The signed message starts with that URL. Append form field names and decoded values, sorted stably by field name without separators. Then append each file-part name and its lowercase SHA1 content digest, sorted stably by part name. Repeated names retain their wire order; do not sort their values or collapse them into a dictionary. Encode the complete message as UTF-8, compute HMAC-SHA1 with the account Callback Token, and compare the lowercase hexadecimal result. This follows [Phaxio's callback verification contract](https://www.phaxio.com/docs/security/callbacks) and `api/app/provider_signatures.py`.

Example form payload (Phaxio):
```
fax[id]=123456&fax[status]=success&fax[num_pages]=2
```

Verification through Faxbot's pure helper (Python; supplied values come from the captured attempt and parsed form):
```python
from api.app.provider_signatures import verify_phaxio_signature

assert verify_phaxio_signature(
    captured_callback_token,
    captured_callback_url_with_locators,
    ordered_form_fields,  # list of (name, decoded string) pairs
    ordered_file_parts,   # list of (part name, file bytes) pairs
    signature_header,
)
```

`PHAXIO_API_SECRET` authenticates send/status API calls and cannot substitute for the Callback Token. Missing/invalid signatures, a missing token, mismatched locators or a captured `PHAXIO_VERIFY_SIGNATURE=false` reject outbound callback updates. Disabling verification never enables unsigned updates. Later configuration changes do not replace a job's captured token, URL or verification setting. Status polling continues with the captured original account when a provider fax ID is available; it does not resubmit the fax. This outbound implementation does not establish that inbound verification has been updated.

The [generated outbound callback source reference](../generated/outbound-callbacks.md) shows this build's exact verifier, URL construction and captured-attempt checks, with source hashes in its provenance.

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

The inbound handler still uses its separate legacy raw-body HMAC-SHA256 implementation. Alignment of inbound verification with the provider contract remains unfinished; do not reuse the corrected outbound helper as evidence of inbound readiness.

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
