
# Scripts and Tests

A practical catalog of helper scripts and core API tests so you can validate Faxbot quickly. Where noted, helpers read values from `.env`. Every API request needs a Faxbot API key. On an existing installation, change server settings in [Settings](../admin-console/settings.md); editing `.env` later does not change them.

## Admin Console workflows

In **Tools → Scripts & Tests**, **Open Keys**, **Open Send Fax**, and **Open Settings** navigate to their existing console workflows. Opening these pages does not create a credential, submit a fax, or save configuration. Review the destination, document and send mode in Send Fax, and check in Settings whether changes are waiting for a restart.

**Show Config** reads configured inbound callback information. It does not prove provider reachability or fax receipt. The separate inbound helper creates a persisted synthetic record; it is not a provider-delivery check. Container actions can affect the host, including tunnels, and remain subject to the installation execution gate.

Disabled sending holds outbound jobs without a provider attempt. It does not simulate delivery, and enabling sending does not automatically transmit held jobs.

## Auth and API basics

- `scripts/run-uvicorn-dev.sh`
    - Starts the API from your working tree (no Docker). Accepts `PORT` (default 8080). Good for rapid iteration.
- `scripts/smoke-auth.sh`
    - Creates a venv, installs API deps, and runs a minimal auth smoke test with pytest.
- `scripts/curl-auth-demo.sh`
    - Hits a running API; mints a DB key via admin endpoint, sends a TXT/PDF fax, then fetches job status.

## Send and status helpers

- `scripts/send-fax.sh "<+15551234567>" /abs/path/file.pdf|.txt`
    - Posts PDF or TXT to `/fax` with a Faxbot API key from `FAXBOT_API_KEY`, or from `API_KEY` in the repository `.env` when `FAXBOT_API_KEY` is not set. It stops with a message if neither is set. `FAX_API_URL` in `.env` sets the server address. A returned job ID means the fax was accepted, not delivered.
- `scripts/get-status.sh <job_id>`
    - Reads `/fax/{id}` with the same key and address rules and prints JSON with `jq`. Reading a job does not send or retry it.

## Inbound helpers

- `scripts/bootstrap-inbound.sh`
    - Legacy bootstrap helper: edits `.env`, starts Compose and invokes the internal inbound smoke. It does not change settings on an existing installation. Configure inbound handling, the internal secret and authentication in Settings first; a synthetic smoke record is not proof of provider receipt or a usable inbound PDF.
- `scripts/inbound-internal-smoke.sh`
    - Posts a simulated internal Asterisk inbound event, lists `/inbound`, and downloads `/inbound/{id}/pdf` using a freshly minted read token.
- `scripts/e2e-inbound-sip.sh`
    - Checks health and Asterisk registration, mints an inbound read token, watches `/inbound` for a new item after you fax to your DID, and downloads the PDF when available.

## Cloud ingress (Phaxio) helper

- `scripts/setup-phaxio-tunnel.sh`
    - Legacy bootstrap helper: starts a tunnel, edits `.env` (`PUBLIC_API_URL`, `PHAXIO_CALLBACK_URL`, `FAX_BACKEND=phaxio`) and stops/restarts Compose. It does not change an existing installation. For an existing installation, start the tunnel manually and apply its URL in Settings as described in [the Phaxio delivery check](phaxio-e2e-test.md).

## Environment and terminal helpers

- `scripts/load-env.sh`
    - Utility to export variables from `.env` into the current shell. Sourced by most scripts.
- `scripts/install-terminal-deps.sh`
    - Installs Python and UI dependencies used by the Admin Console’s Terminal feature. See Terminal guide.

## Release (maintainers)

- `scripts/release_npm.sh`
    - Publishes Node packages (`node_mcp`, `sdks/node`) to npm. Requires `npm login` or `NPM_TOKEN`.
- `scripts/release_pypi.sh`
    - Builds and uploads Python packages (`sdks/python`, `python_mcp`) to PyPI. Requires `twine` auth.

## Node MCP scripts
Helper scripts for the Node MCP server (AI assistant integration). Requires a running Faxbot API; inherits `FAX_API_URL` and `API_KEY` from your environment. Only the stdio server uses `API_KEY`; the Streamable HTTP server uses each client's own Faxbot key.

- `node_mcp/scripts/start-stdio.sh`
    - Launches stdio transport (`src/servers/stdio.js`). Best for desktop assistants; uses `filePath` (no base64).
- `node_mcp/scripts/start-http.sh`
    - Launches the Streamable HTTP server on port 3001 (`src/servers/http.js`).
- The Node package has no SSE server; use `python_mcp/server.py` for SSE-only clients.
- `node_mcp/scripts/test-stdio.js "<to>" <filePath>`
    - Spawns the stdio server and calls the `send_fax` tool using a local file path. Example:
        - `node node_mcp/scripts/test-stdio.js "+15551234567" /abs/path/sample.pdf`
- `node_mcp/scripts/call-send-fax.js "<to>" <filePath>`
    - Calls the `send_fax` tool handler directly (bypasses transport) for quick local testing.

Notes

- Streamable HTTP and SSE send files as base64 within a 16 MB JSON body limit, so base64 overhead counts toward that limit. REST uploads use the active configured raw-file limit (default 10 MB).
- Prefer stdio + `filePath` to avoid base64 overhead for desktop integrations.

## API tests overview

Follow the maintained [API Tests Overview](api-tests.md) for isolated development-environment commands and the coverage map. The production image does not bundle the suite, so the legacy `make test` target is not a supported suite runner.

For operator checks, [disabled sending](../setup/test-mode.md) accepts real documents as held jobs without issuing a provider attempt. Held jobs never transmit automatically when sending is enabled and cannot be marked delivered by fabricated callbacks.
