# Faxbot Python SDK

Thin Python client for the Faxbot API. Sends faxes and checks status via the unified Faxbot REST API (independent of the server’s backend: Phaxio or SIP/Asterisk).

- Package name: `faxbot`
- Requires: Python 3.7+

## Install

- From PyPI (once published):
```
pip install faxbot
```
- From source (this repo):
```
cd sdks/python
pip install .
```

## Usage
```python
from faxbot import FaxbotClient

client = FaxbotClient(base_url="http://localhost:8080", api_key="YOUR_API_KEY")
job = client.send_fax("+15551234567", "/path/to/document.pdf")
print("Queued job:", job["id"], job["status"]) 
status = client.get_status(job["id"])
print("Status:", status["status"]) 
```

## Sending a fax once

Each `send_fax` call is one fax, identified by an operation id that the client sends as the `Idempotency-Key` header. Without `operation_id`, the call is a new fax and gets a new id, even for a document you sent before.

If the connection drops, times out, or Faxbot answers 502, 503 or 504, the client sends the same fax again with the same id, up to `retries` more times (default 0, so it never sends again on its own; waiting `retry_backoff` seconds and then twice as long each time). Faxbot answers with the original job instead of sending the fax twice. A 4xx answer is never retried.

If no attempt is confirmed, `send_fax` raises `FaxSubmissionUncertain`. Faxbot may or may not have the fax. Call `resume_fax` with the error's `operation_id` and the same number and document to finish that same fax:

```python
from faxbot import FaxbotClient, FaxSubmissionUncertain

client = FaxbotClient("http://localhost:8080", "YOUR_API_KEY")
operation_id = FaxbotClient.new_operation_id()
save_somewhere(operation_id)  # your own storage; the SDK keeps nothing on disk
try:
    job = client.send_fax("+15551234567", "letter.pdf", operation_id=operation_id)
except FaxSubmissionUncertain as error:
    # Now, or after a restart with the saved id:
    job = client.resume_fax(error.operation_id, "+15551234567", "letter.pdf")
```

- The same id with a different number or document raises `FaxOperationConflict` (HTTP 409).
- The SDK keeps no record of ids or documents. To finish a fax after your program restarts, save the id before sending, as above.
- Pass `session=` to use your own `requests.Session` for every request. The API key is sent with each request and never stored on the session.
- Faxbot servers released before Idempotency-Key support ignore the header, so a retry after a lost response can send the fax twice. Keep the default `retries=0` against those servers and do not resume an unconfirmed send there; check the job list instead.

## Notes
- Only `.pdf` and `.txt` files are accepted.
- If the server requires an API key, it must be supplied via `X-API-Key` (handled automatically when `api_key` is provided).
- Optional helper: `check_health()` pings `/health`.

## Publishing (maintainers)
- Configure GitHub secret `PYPI_API_TOKEN`.
- Create a GitHub Release to trigger publish via CI.

## MCP Note
- MCP (Model Context Protocol) is not part of this SDK. It is a separate integration layer for AI assistants.
- Refer to `docs/mcp/index.md` in the repository for MCP setup and usage.
