---
layout: default
title: API Keys
parent: Admin Console
nav_order: 4
permalink: /admin-console/api-keys/
---

# API Keys

Manage and test REST API credentials without leaving the Admin Console.

## Create & rotate keys

1. Open **Admin Console → API Keys**
2. Click **Create Key** and choose scopes (fax:send, inbound:list, admin, etc.)
3. Copy the generated token (`fbk_live_<id>_<secret>`) — it is only shown once
4. Rotate or revoke from the same screen; Faxbot records the change in audit logs when enabled

{: .note }
For production, set `REQUIRE_API_KEY=true` in the Setup Wizard or Security tab so unauthenticated requests are rejected.

## Smoke test from the console

- Use **Send Fax** to queue a test while the new key is active
- View the auto-generated curl example in the sidebar if you need to script verification
- Diagnostics → **API Auth** lists recent successes/failures with reasons

### Optional CLI validation

If you still want to confirm via terminal, copy the curl snippet shown in the console or run:

```bash
curl -X POST "$BASE/fax" \
  -H "X-API-Key: $API_KEY" \
  -F to=+15551234567 \
  -F file=@./document.pdf
```

Then check status:

```bash
curl -H "X-API-Key: $API_KEY" "$BASE/fax/$JOB_ID"
```

## Troubleshooting

- **401 Unauthorized** → Scope missing or wrong key. Reissue the key with `fax:send`/`fax:read` scopes.
- **Rate limited** → Global limit hit; adjust `MAX_REQUESTS_PER_MINUTE` in **Settings → Security**.
- **413 / 415** → File too large or wrong type; review [Images & PDFs]({{ site.baseurl }}/backends/images-and-pdfs.html).

For automation examples, see the [Node SDK]({{ site.baseurl }}/development/node-sdk.html) and [Python SDK]({{ site.baseurl }}/development/python-sdk.html).
