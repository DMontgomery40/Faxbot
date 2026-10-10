
# API Keys

API keys let apps, scanners and phones use Faxbot. Every API request needs a key or a signed-in console session; there is no anonymous access. Managing keys needs the `keys:manage` permission.

## Create, replace and revoke keys

1. Open **Administration → Keys & phones**.
2. Create a key, choose the user or integration it belongs to, and pick only the permissions the app needs, for example `fax:send` and `fax:read`.
3. Copy the key (`fbk_live_<id>_<secret>`). It is shown only once.
4. Use **Replace key** to issue a new secret, or **Revoke** to stop the key for good. Faxbot records both in the security audit.

A key can never do more than the user or integration it belongs to. See [Access Control](../security/access-control.md#keys) for expiry, keys that need review, and the keys the iPhone app receives.

## Smoke test from the console

- Use **Faxes → Send a fax** to queue a test while the new key is active
- View the auto-generated curl example in the sidebar if you need to script verification
- Diagnostics reports active installation readiness; it does not test a newly created key or provide an API Auth history panel.

### Quick examples

=== "Console"

    1. Open **Administration → Keys & phones**  
    2. Create a key with the permissions you need (for example `fax:send` and `fax:read`)  
    3. Sign in with that key (**Sign in with API key**) and queue a test on **Send a fax**  
    4. Check **Sent** for status updates

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

- **401 Unauthorized** → The key is missing, mistyped, expired or revoked, or its owner is disabled.
- **403 Forbidden** → The key is valid but lacks the permission. Create a key with the permission you need; its owner must also have it.
- **429 Too Many Requests** → The per-key limit was reached; adjust **Requests per minute for each key** under **Administration → Documents & retention**.
- **413 / 415** → File too large or wrong type; review [document submission](../api.md#submit-a-document).

For automation examples, see the [Node SDK](../sdks/node.md) and [Python SDK](../sdks/python.md).
