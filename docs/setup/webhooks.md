
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

`PHAXIO_API_SECRET` authenticates send/status API calls and cannot substitute for the Callback Token. Missing/invalid signatures, a missing token, mismatched locators or a captured `PHAXIO_VERIFY_SIGNATURE=false` reject outbound callback updates. Disabling verification never enables unsigned updates. Later configuration changes do not replace a job's captured token, URL or verification setting. Status polling continues with the captured original account when a provider fax ID is available; it does not resubmit the fax. Received-fax notifications use the same verifier; see [Receiving faxes](../operations/receiving.md).

The [generated outbound callback source reference](../generated/outbound-callbacks.md) shows this build's exact verifier, URL construction and captured-attempt checks, with source hashes in its provenance.

## Inbound — received faxes

[Receiving faxes](../operations/receiving.md) describes the whole flow: statuses, **Fetch again**, test faxes and the evidence Faxbot keeps. In short, nothing is recorded until a notification is checked. The document always comes from the provider's API by fax ID, or from an image inside the data folder, never from an address in the notification.

### Phaxio {#inbound-phaxio}

- Endpoint: `POST /phaxio-inbound`. The signed URL is `PUBLIC_API_URL` followed by `/phaxio-inbound`.
- Signature: `X-Phaxio-Signature`, the same lowercase hexadecimal HMAC-SHA1 with `PHAXIO_CALLBACK_TOKEN` as outbound callbacks, over that URL, the sorted form fields and the SHA-1 digest of each file part.
- With `PHAXIO_INBOUND_VERIFY_SIGNATURE=false`, Faxbot looks the fax up with `GET https://api.phaxio.com/v2.1/faxes/{id}` in the configured account before recording it, and ignores it when the account did not receive it.

Phaxio sends a multipart form. The fax object arrives as a JSON `fax` field, and the PDF arrives in a `file` part when files are sent with callbacks. A shortened example:

```
fax={"id":98765,"direction":"received","num_pages":3,"status":"success","from_number":"+15551230000","to_number":"+15559870000","completed_at":"2026-10-03T08:00:00.000-06:00"}
direction=received
is_test=false
success=true
file=<the PDF>
```

When there is no `file` part, Faxbot downloads `GET https://api.phaxio.com/v2.1/faxes/{id}/file`.

### Sinch Fax API v3 {#inbound-sinch-fax-api-v3}

- Endpoint: `POST /sinch-inbound`
- Basic auth: `SINCH_INBOUND_BASIC_USER` and `SINCH_INBOUND_BASIC_PASS`. HMAC: `X-Sinch-Signature` with `SINCH_INBOUND_HMAC_SECRET`.
- With neither set, Faxbot looks the fax up with `GET /v3/projects/{projectId}/faxes/{id}` before recording it. It downloads the document from `/file`.

A shortened example of an incoming fax event:

```json
{
  "event": "INCOMING_FAX",
  "eventTime": "2026-10-03T14:00:01Z",
  "fax": {
    "id": "01F3J0G1M4WQR6HGY6HCF6JA0K",
    "direction": "INBOUND",
    "from": "+15551230000",
    "to": "+15559870000",
    "numberOfPages": 2,
    "status": "COMPLETED",
    "completedTime": "2026-10-03T14:00:00Z"
  },
  "file": "<base64 PDF>",
  "fileType": "PDF"
}
```

Faxbot uses an attached `file` only when basic auth or HMAC authenticated the event.

### SIP/Asterisk (self-hosted) {#inbound-sipasterisk-selfhosted}

- Endpoint: `POST /_internal/asterisk/inbound`
- Header: `X-Internal-Secret: <ASTERISK_INBOUND_SECRET>`
- `tiff_path` must be inside the Faxbot data folder. Paths through a symbolic link are refused. A repeated `uniqueid` is the same fax.

Body (JSON):

```json
{
  "tiff_path": "/faxdata/inbound/1603261234.89.tiff",
  "to_number": "+15559870000",
  "from_number": "+15551230000",
  "faxstatus": "SUCCESS",
  "faxpages": 2,
  "uniqueid": "1603261234.89"
}
```

## Security Tips

- Use HTTPS for all public callbacks.
- Keep secrets out of logs; audit only metadata (job ids, event types).
- Rotate webhook secrets periodically and validate signatures strictly.
