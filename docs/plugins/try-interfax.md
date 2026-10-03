# InterFAX Manifest and Adapter Requirements

InterFAX requires adapter work before this recipe can perform a tracked real send. Its documented protocol returns the new fax identity in the response `Location` header and reports numeric status values. The current generic HTTP-manifest runtime requires a JSON response identity and a string status; the draft below cannot bridge those requirements by itself.

The mandatory adapter must preserve the identity returned by InterFAX and normalize its status before enabling this outbound path. The official [Python client](https://github.com/interfax/interfax-python/blob/master/interfax/client.py), [outbound adapter](https://github.com/interfax/interfax-python/blob/master/interfax/outbound.py), and [fax response schema](https://rest.interfax.net/outbound/help/operations/FaxGetFull) document the protocol. This is an implementation requirement, not a waiver of InterFAX support.

## Prerequisites

On an existing installation, open Settings, turn on v3 plugins and apply. If Settings asks for a restart, stop every Faxbot API process and start the installation again. `FEATURE_V3_PLUGINS=true` in the environment only applies when a new installation starts for the first time.

## 1) Prepare the manifest definition

The manifest is a provider definition, not a secret/runtime settings store. This valid JSON records the request endpoints and the supported status context. Keep this draft out of the normal outbound selection until the required response-header identity and numeric status handling have been implemented and reviewed.

Create `config/providers/interfax/manifest.json` on the API host:

```json
{
  "id": "interfax",
  "name": "InterFAX API",
  "kind": "cloud",
  "auth": { "scheme": "basic" },
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
      "url": "https://rest.interfax.net/outbound/faxes?faxNumber={{to}}",
      "headers": {
        "Content-Location": "{{file_url}}",
        "Content-Type": "application/pdf"
      },
      "body": { "kind": "none", "template": "" },
      "response": { "job_id": "id" }
    },
    "get_status": {
      "method": "GET",
      "url": "https://rest.interfax.net/outbound/faxes/{{provider_sid}}",
      "headers": { "Accept": "application/json" },
      "body": { "kind": "none", "template": "" },
      "response": { "status": "status" }
    }
  },
  "allowed_domains": ["rest.interfax.net"]
}
```

The `job_id` and `status` response entries above describe the current JSON-field mapping format. They do not extract InterFAX's `Location` header or normalize its numeric status.

## 2) Configure credentials

In Admin Console → Plugins → `interfax`, set Basic-auth credentials with `{"username":"YOUR_INTERFAX_USERNAME","password":"YOUR_INTERFAX_PASSWORD"}`. Leave hidden credentials unchanged unless you are replacing them. Save, and restart if Faxbot asks for it. Enabling the plugin does not complete the adapter requirements.

For local document checks, use a supported provider with sending actively disabled and inspect held artifacts. Held work is not simulated success and never automatically dispatches when sending is enabled.

## 3) Controlled test after adapter completion

After the required adapter has been implemented and reviewed, enable InterFAX for outbound, inspect active/pending status, complete any required installation-wide stop/start, and confirm sending is enabled. Use a synthetic document and a destination you control through Admin Console → Send Fax or the tracked API:

```bash
BASE="http://localhost:8080"; API_KEY="your_api_key"
curl -X POST "$BASE/fax" -H "X-API-Key: $API_KEY" -F to=+15551234567 -F file=@./example.pdf
```

Manifest validation only checks the definition. A returned fax job ID is acceptance. Inspect the issued attempt and original provider account, and verify the received pages and content for delivery; reconcile uncertain outcomes before submitting another request.
