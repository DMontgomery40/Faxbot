---
layout: default
title: Node.js SDK
parent: SDKs
nav_order: 1
permalink: /sdks/node/
---

# Node.js SDK

Install
- Local project includes the SDK in `sdks/node/`. For package usage, require the client class directly.

Usage
```javascript
const FaxbotClient = require('faxbot');
const client = new FaxbotClient('http://localhost:8080', 'your_api_key');

// Send a fax
const job = await client.sendFax('+15551234567', '/path/to/document.pdf');
// Later
const status = await client.getStatus(job.id);
```

Errors
- 400: invalid phone or params
- 401: missing/invalid API key
- 404: job or endpoint not found
- 413: file too large
- 415: unsupported type

Health check
```javascript
const ok = await client.checkHealth();
```

