# MCP Transports

## Per-client keys

Streamable HTTP and SSE requests must each carry the caller's own Faxbot API key, as `Authorization: Bearer <key>` or `X-API-Key: <key>`. The MCP server forwards that key to Faxbot as `X-API-Key`, so every fax is sent and read with the permissions of the key's owner. Requests without a key get 401 and never reach Faxbot. With OAuth configured (`OAUTH_ISSUER`, `OAUTH_AUDIENCE`), the Bearer token must be a JWT from that issuer, and its `sub` is looked up in the JSON file named by `MCP_OAUTH_SUBJECT_KEYS_FILE` (`{"<subject>": "<faxbot api key>"}`); unmapped subjects get 403. Stdio uses `API_KEY` as one integration identity.

## Streamable HTTP (Node and Python)

:material-lan: Ports
: `3001` (Node), `3004` (Python), or embedded at `/mcp/http/mcp`

:material-shield-lock: Auth
: The caller's Faxbot key, or OAuth with a subject-to-key map. `MCP_RESOURCE_URL` publishes OAuth protected-resource metadata

:material-cloud-lock: Deployment
: Run behind TLS. Set `MCP_ALLOWED_HOSTS` to the public host names. Browser origins are refused unless listed in `MCP_ALLOWED_ORIGINS`

## SSE (Python only, compatibility)

:material-lan-connect: Port
: `3003`, or embedded at `/mcp/sse/sse`

:material-shield-key: Auth
: Same as Streamable HTTP. The key must be sent on both `GET /sse` and `POST /messages/`

## WebSocket (Node development bridge)

:material-websocket: Port
: `3004` (default)

:material-shield-lock: Auth
: `MCP_WS_API_KEY` (or `API_KEY`) in a header. Connections are refused when neither is set, and keys in the URL are refused

## Stdio

:material-console-line: Use case
: Best for desktop assistants; avoids base64 limits

:material-file: Files
: Prefer `filePath` for fidelity

## Limits

:material-upload: REST API
: Raw file limit `MAX_FILE_SIZE_MB` (default 10 MB)

:material-code-json: Streamable HTTP and SSE
: Request body limit 16 MB (base64 payload)
