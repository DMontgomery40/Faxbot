
# API Keys

Manage and test REST API credentials without leaving the Admin Console.

## Create & rotate keys

1. Open **Admin Console → API Keys**
2. Click **Create Key** and choose scopes (fax:send, inbound:list, admin, etc.)
3. Copy the generated token (`fbk_live_<id>_<secret>`) — it is only shown once
4. Rotate or revoke from the same screen; Faxbot records the change in audit logs when enabled

!!! note
    For production, set `REQUIRE_API_KEY=true` in the Setup Wizard or Security tab so unauthenticated requests are rejected.

## Smoke test from the console

- Use **Send Fax** to queue a test while the new key is active
- View the auto-generated curl example in the sidebar if you need to script verification
- Diagnostics reports active installation readiness; it does not test a newly created key or provide an API Auth history panel.

### Quick examples

=== "Console"

    1. Open Admin Console → API Keys  
    2. Create a key with the scopes you need (e.g., `fax:send`, `fax:read`)  
    3. Open Send Fax and queue a test while the key is selected  
    4. Check Jobs for status updates

=== "curl"

    ```bash
    BASE="http://localhost:8080"
    curl -X POST "$BASE/fax" \
      -H "X-API-Key: $API_KEY" \
      -F to=+15551234567 \
      -F file=@./document.pdf
    
    # Then check status
    curl -H "X-API-Key: $API_KEY" "$BASE/fax/$JOB_ID"
    ```

=== "Node"

    ```js
    const FaxbotClient = require('faxbot');
    const client = new FaxbotClient('http://localhost:8080', process.env.API_KEY);
    (async () => {
      const job = await client.sendFax('+15551234567', './document.pdf');
      console.log('Queued:', job.id);
      const status = await client.getStatus(job.id);
      console.log('Status:', status.status);
    })();
    ```

=== "Python"

    ```python
    from faxbot import FaxbotClient
    client = FaxbotClient('http://localhost:8080', api_key=os.getenv('API_KEY'))
    job = client.send_fax('+15551234567', './document.pdf')
    print('Queued', job['id'])
    status = client.get_status(job['id'])
    print('Status', status['status'])
    ```

## Troubleshooting

- **401 Unauthorized** → Scope missing or wrong key. Reissue the key with `fax:send`/`fax:read` scopes.
- **Rate limited** → Global limit hit; adjust `MAX_REQUESTS_PER_MINUTE` in **Settings → Security**.
- **413 / 415** → File too large or wrong type; review [Images & PDFs](../guides/images-and-pdfs.md).

For automation examples, see the [Node SDK](../sdks/node.md) and [Python SDK](../sdks/python.md).
