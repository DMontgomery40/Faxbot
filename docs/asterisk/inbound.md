---
layout: default
title: Asterisk Inbound
nav_order: 20
permalink: /asterisk/inbound/
---

# Asterisk Inbound (SIP/T.38)

Use this guide to deliver inbound fax metadata from your Asterisk dialplan to Faxbot.

Requirements
- Faxbot API reachable on your private network (do not expose `/ _internal` endpoints publicly)
- Shared secret configured in Faxbot (`ASTERISK_INBOUND_SECRET`)
- Asterisk with T.38 support (ReceiveFAX) and the ability to invoke `curl` or `System()`

Configure Faxbot secret
- In the Admin Console → Settings → Inbound Receiving (SIP), click “Generate” on “Asterisk Inbound Secret”.
- This value is only for your dialplan → Faxbot; keep it private.

Dialplan example
```
; Save incoming TIFF
exten => fax,1,NoOp(Receiving fax)
 same => n,Set(TIFFPATH=/var/spool/asterisk/fax/${UNIQUEID}.tiff)
 same => n,ReceiveFAX(${TIFFPATH})

; Post inbound metadata to Faxbot (private network)
 same => n,System(curl -s -H "X-Internal-Secret: YOUR_SECRET_HERE" \
    -H "Content-Type: application/json" \
    -d '{"tiff_path":"${TIFFPATH}","to_number":"${EXTEN}","from_number":"${CALLERID(num)}","faxstatus":"${FAXSTATUS}","faxpages":"${FAXPAGES}","uniqueid":"${UNIQUEID}"}' \
    http://api:8080/_internal/asterisk/inbound)
 same => n,Hangup()
```

Notes
- Replace `YOUR_SECRET_HERE` with the generated secret from Faxbot.
- Replace `api:8080` with your Faxbot API service DNS/IP inside your private network.
- Keep port 5038 (AMI) private; never expose it on the public internet.

Troubleshooting
- Admin Console → Logs: filter with `event:inbound_received` to confirm receipt.
- Diagnostics → SIP/Asterisk: use “Help” for AMI/network guidance.

