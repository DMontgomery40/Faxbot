
# Network & Transports

Guidance for securing MCP transports and webhooks when running Faxbot in production.

## MCP Transports

- Streamable HTTP (Node and Python MCP)
  - Ports: 3001 (Node), 3004 (Python), or built into the API at `/mcp/http/mcp` with `ENABLE_MCP_HTTP=true`.
  - Each client sends its own Faxbot API key as `Authorization: Bearer <key>` or `X-API-Key`, and the server uses it for that client's requests. Give each client its own key with only the permissions it needs. Requests without a key are refused.
  - The server built into the API follows the same rule: it never uses the installation key (`API_KEY`) for tool calls. Its host, origin and OAuth settings come from the API's environment, not from Settings.
  - For OAuth, set `OAUTH_ISSUER`, `OAUTH_AUDIENCE`, optionally `OAUTH_JWKS_URL`, and map token subjects to Faxbot keys in `MCP_OAUTH_SUBJECT_KEYS_FILE`.
  - Set `MCP_ALLOWED_HOSTS` to your public host names. Browser origins are refused unless listed in `MCP_ALLOWED_ORIGINS`.
  - Run behind TLS via a reverse proxy; add IP allowlists and rate limits where appropriate.

- SSE (Python MCP only, for older clients)
  - Port: 3003, or built into the API at `/mcp/sse/sse` with `ENABLE_MCP_SSE=true`.
  - Same per-client keys and OAuth options as Streamable HTTP. Prefer Streamable HTTP for new clients.

## Webhooks & Callbacks

- Phaxio (outbound status)
  - Endpoint: `POST /phaxio-callback?job_id=<job_id>&attempt_id=<attempt_id>`; Faxbot adds these locators to the submitted callback URL.
  - Signature: `X-Phaxio-Signature`, a lowercase hexadecimal HMAC-SHA1 using the separate account `PHAXIO_CALLBACK_TOKEN`.
  - Verification covers the exact captured public URL and query, followed by stably name-sorted form fields and file-part SHA1 digests. The API secret authenticates send/status API calls; it is not the callback token.
  - A job captured with `PHAXIO_VERIFY_SIGNATURE=false` rejects outbound callback updates. Status polling continues through its captured original account when a provider fax ID is available.
  - See [outbound callback verification](../setup/webhooks.md#outbound-status-phaxio) for ordering and correlation details.
  - Always use HTTPS public URLs; avoid exposing staging/test endpoints publicly.

- Phaxio (inbound)
  - Endpoint: `POST /phaxio-inbound`
  - Signature: `X-Phaxio-Signature`, the same HMAC-SHA1 with `PHAXIO_CALLBACK_TOKEN` as outbound callbacks. With checks off, Faxbot confirms each fax with Phaxio's API before recording it. See [Receiving faxes](../operations/receiving.md#how-notifications-are-checked).

- Sinch (inbound)
  - Endpoint: `POST /sinch-inbound`
  - Basic auth: `SINCH_INBOUND_BASIC_USER/PASS`
  - Sinch Fax API v3 does not sign webhooks. Without both Basic auth values, Faxbot confirms each fax with Sinch's API before recording it.

- SIP/Asterisk (inbound)
  - Endpoint: `POST /_internal/asterisk/inbound`
  - Header: `X-Internal-Secret: <ASTERISK_INBOUND_SECRET>`
  - Only accessible over private networks; do not expose publicly.

## Reverse Proxy Recommendations

- Enforce TLS; redirect HTTP→HTTPS.
- Set security headers (HSTS, CSP, X-Content-Type-Options, Referrer-Policy, X-Frame-Options, Permissions-Policy).
- Limit request sizes; apply rate limits and IP restrictions as needed.
- Do not log PHI; log IDs and generic metadata only.
