
# Faxbot Python SDK

Thin Python client for the Faxbot API. Sends faxes and checks status via the unified Faxbot REST API (independent of the server's backend: Phaxio or SIP/Asterisk).

The current source package is proprietary and requires prior written permission from David Montgomery for use. Previously released MIT versions retain their existing permissions. See the [license](https://github.com/DMontgomery40/Faxbot/blob/main/LICENSE).

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
The package metadata says not to upload this package to PyPI. Distribution requires prior written permission under the [Faxbot license](https://github.com/DMontgomery40/Faxbot/blob/main/LICENSE).

## MCP Note
- MCP (Model Context Protocol) is not part of this SDK. It is a separate integration layer for AI assistants.
- See [MCP integration](../mcp/index.md) for MCP setup and usage.
