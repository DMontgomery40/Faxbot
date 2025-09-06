# TROUBLESHOOTING.md

## General
- 401 Invalid API key: set `API_KEY` and pass `X-API-Key` header.
- 413 File too large: adjust `MAX_FILE_SIZE_MB`.
- 415 Unsupported file type: only PDF and TXT allowed.
- Prefer HTTPS for `PUBLIC_API_URL` in production. The cloud backend fetches PDFs from your server; use TLS.

## Phaxio Backend
- "phaxio not configured": ensure `FAX_BACKEND=phaxio`, `PHAXIO_API_KEY`, `PHAXIO_API_SECRET`.
- No status updates: verify your callback URL (`PHAXIO_CALLBACK_URL` or `PHAXIO_STATUS_CALLBACK_URL`) and that your server is publicly reachable.
- 403 on `/fax/{id}/pdf`: invalid token or wrong `PUBLIC_API_URL`.
- Phaxio API error: confirm credentials and sufficient account balance.

## SIP/Asterisk Backend
- AMI connection failed:
  - Asterisk container running and reachable on `5038`.
  - `ASTERISK_AMI_*` match `manager.conf` template.
  - API logs show reconnect with exponential backoff.
- T.38 negotiation failed:
  - Provider supports UDPTL.
  - Firewall forwards UDP `4000-4999`.
  - `pjsip.conf` has `t38_udptl=yes` and redundancy.
- No fax send:
  - Check Asterisk logs for `SendFAX` and `FaxResult` events.
  - Validate destination formatting.

## Conversion
- Ghostscript missing: API warns and stubs TIFF conversion; install `ghostscript` for production.
- Garbled pages: use clean PDF fonts or provide PDF instead of TXT.

## Reverse Proxy Examples (Rate Limiting)

Nginx (basic example):
```
server {
  listen 443 ssl;
  server_name your-domain.com;

  # ... ssl_certificate / ssl_certificate_key ...

  # Simple rate limit per IP
  limit_req_zone $binary_remote_addr zone=faxbot:10m rate=5r/s;

  location / {
    limit_req zone=faxbot burst=10 nodelay;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header Host $host;
    proxy_pass http://127.0.0.1:8080;
  }
}
```

Caddy (basic example):
```
your-domain.com {
  reverse_proxy 127.0.0.1:8080
  # Rate limiting plugins vary; consider layer-4 or WAF if needed
}
```
