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

## Notes
- Only `.pdf` and `.txt` files are accepted.
- If the server requires an API key, it must be supplied via `X-API-Key` (handled automatically when `api_key` is provided).
- Optional helper: `check_health()` pings `/health`.

## Publishing (maintainers)
- Configure GitHub secret `PYPI_API_TOKEN`.
- Create a GitHub Release to trigger publish via CI.

## MCP Note
- MCP (Model Context Protocol) is not part of this SDK. It is a separate integration layer for AI assistants.
- Refer to `docs/MCP_INTEGRATION.md` in the repository for MCP setup and usage.
