# Try It: RingCentral Manifest

This walkthrough adds a RingCentral manifest and uses Faxbot's tracked Send flow for a controlled fax. It follows RingCentral's documented [multipart form-data format](https://developers.ringcentral.com/guide/messaging/fax/fax-multipart-formats) and [extension-scoped send and status endpoints](https://developers.ringcentral.com/guide/messaging/fax/sending-faxes).

## Prerequisites

Use a RingCentral account and OAuth access token with fax permission for the selected extension. RingCentral uses the extension's configured outbound fax number.

On an existing installation, load Settings, enable v3 plugins and apply with the loaded desired revision. Inspect active/pending status and complete an installation-wide stop/start when pending. `FEATURE_V3_PLUGINS=true` is a first-bootstrap environment input, not an import into an initialized canonical store.

## 1) Create the manifest file

The manifest is a provider definition, not a secret/runtime settings store. Create `config/providers/ringcentral/manifest.json` on the API host:

```json
{
  "id": "ringcentral",
  "name": "RingCentral Fax API",
  "kind": "cloud",
  "auth": { "scheme": "bearer" },
  "traits": {
    "requires_ghostscript": true,
    "requires_tiff": false,
    "supports_inbound": false,
    "inbound_verification": "none",
    "needs_storage": false,
    "outbound_status_only": false
  },
  "actions": {
    "send_fax": {
      "method": "POST",
      "url": "https://platform.ringcentral.com/restapi/v1.0/account/~/extension/~/fax",
      "body": {
        "kind": "multipart",
        "template": "to={{to}}&faxResolution=High&attachment={{file}}"
      },
      "response": {
        "job_id": "id",
        "status": "messageStatus",
        "status_map": {
          "Queued": "in_progress",
          "Sent": "success",
          "SendingFailed": "failed"
        }
      }
    },
    "get_status": {
      "method": "GET",
      "url": "https://platform.ringcentral.com/restapi/v1.0/account/~/extension/~/message-store/{{provider_sid}}",
      "body": { "kind": "none", "template": "" },
      "response": {
        "job_id": "id",
        "status": "messageStatus",
        "status_map": {
          "Queued": "in_progress",
          "Sent": "success",
          "SendingFailed": "failed"
        }
      }
    }
  },
  "allowed_domains": ["platform.ringcentral.com"],
  "timeout_ms": 15000
}
```

The multipart template supplies separate `to` and `faxResolution` form fields and a PDF `attachment`; the HTTP client creates the multipart boundary. RingCentral's documented fax states are [Queued, Sent and SendingFailed](https://developers.ringcentral.com/guide/messaging/message-store/messaging). Unknown responses remain uncertain and require reconciliation.

Keep the token in canonical plugin credentials through Admin Console. Bearer authentication accepts the `token` or `api_key` setting. This manifest does not obtain or refresh OAuth tokens.

## 2) Enable + configure in Admin Console

1. Open Admin Console → Plugins.
2. Select `ringcentral` and enable its outbound role.
3. Set plugin credentials with `{"token":"YOUR_RINGCENTRAL_OAUTH_ACCESS_TOKEN"}`. Omit unchanged masked credentials.
4. Save with the loaded desired revision, inspect active/pending status and confirm the active outbound provider. Disabled sending accepts held jobs that never automatically dispatch.

Alternatively, first read `/plugins/ringcentral/config` and retain `_meta.desired_revision_id`; include it in the write. A conflict requires explicit reload/review, not a silent fresh revision just before mutation:

```bash
BASE="http://localhost:8080"; API_KEY="your_admin_api_key"
curl -sS -X PUT "$BASE/plugins/ringcentral/config" \
  -H "X-API-Key: $API_KEY" -H 'content-type: application/json' \
  -d '{"expected_revision_id":"LOADED_DESIRED_REVISION","enabled":true,"role":"outbound","settings":{"token":"YOUR_RINGCENTRAL_OAUTH_ACCESS_TOKEN"}}'
```

## 3) Send a controlled test

Confirm RingCentral is active for outbound and sending is enabled. Use a synthetic document and a destination you control in Admin Console → Send Fax, or run:

```bash
BASE="http://localhost:8080"; API_KEY="your_api_key"
curl -X POST "$BASE/fax" \
  -H "X-API-Key: $API_KEY" \
  -F to=+15551234567 \
  -F file=@./example.pdf
```

Then check status using the returned job ID:

```bash
curl -H "X-API-Key: $API_KEY" "$BASE/fax/$JOB_ID"
```

Manifest validation only checks the provider definition; Send creates a tracked job. A returned fax job ID is acceptance. Inspect the issued attempt and original provider account, and verify the received pages and content for delivery; reconcile uncertain outcomes before submitting another request.
