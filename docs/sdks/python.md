---
layout: default
title: Python SDK
parent: SDKs
nav_order: 2
permalink: /sdks/python/
---

# Python SDK

Install
- Local project includes the SDK in `sdks/python/`.

Usage
```python
from faxbot import FaxbotClient

client = FaxbotClient('http://localhost:8080', 'your_api_key')
job = client.send_fax('+15551234567', '/path/to/document.pdf')
status = client.get_status(job['id'])
ok = client.check_health()
```

Errors
- 400: invalid phone or params
- 401: missing/invalid API key
- 404: job not found
- 413: file too large
- 415: unsupported type

