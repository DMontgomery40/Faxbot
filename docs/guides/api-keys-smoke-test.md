---
layout: default
title: API Keys and Smoke Test
parent: Guides
nav_order: 4
permalink: /guides/api-keys-smoke-test/
---

# API Keys and Smoke Test

Enable API key requirement
- In Settings → Security, set `API_KEY`
- Clients must send `X-API-Key: <value>`

Smoke test (curl)
```
curl -X POST "$BASE/fax" \
  -H "X-API-Key: $API_KEY" \
  -F to=+15551234567 \
  -F file=@./document.pdf
```
Then:
```
curl -H "X-API-Key: $API_KEY" "$BASE/fax/$JOB_ID"
```

SDK test
- [Node](/Faxbot/sdks/node/) and [Python](/Faxbot/sdks/python/) include error mappings for 400/401/404/413/415

Troubleshooting
- 401: header missing or incorrect value
- 415: only PDF/TXT allowed
- 413: file exceeds `MAX_FILE_SIZE_MB`

