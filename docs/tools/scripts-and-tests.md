
# Scripts and Tests

A practical catalog of helper scripts and core API tests so you can validate Faxbot quickly. Where noted, helpers read values from `.env`. Every API request needs a Faxbot API key. On an existing installation, change server settings in [Settings](../admin-console/settings.md); editing `.env` later does not change them.

## In the console

**System → Developer → Scripts & checks** has tools for checking Faxbot by hand. None of them sends a fax or changes a setting.

- **Add a test fax** puts a one-page fax in Received, marked as a test everywhere. It goes through owners, mailbox rules and email delivery like a real fax; no call is made. It needs receiving to be on.
- **How the receiving provider reaches Faxbot** shows the address to give a provider that calls Faxbot when a fax arrives (Phaxio, Sinch, SignalWire, eFax notifications), with a copy button and the provider's guide. For a carrier trunk it shows whether received faxes reach Faxbot; for a provider Faxbot collects from (HumbleFax), it says there is nothing to set.
- **Fax engine** lists what Faxbot's own fax engine reports now: trunk sign-ins, the addresses it checks, calls in progress and faxes in progress. The command line has the same lists: `faxbot system diagnostics engine registrations|contacts|calls|faxes`.
- **Server checks** run read-only commands on Faxbot's server (Python version, document converter version, files in the fax data folder) when the installation allows it (`ENABLE_ADMIN_EXEC`). The command line equivalent is `faxbot system actions run <id>`.

For "is everything working", use [Diagnostics](../admin-console/diagnostics.md).

## Auth and API basics

- `scripts/run-uvicorn-dev.sh`
    - Starts the API from your working tree (no Docker) on `127.0.0.1` with sending turned off. Accepts `PORT` (default 8080).
- `scripts/smoke-auth.sh`
    - Runs the API key tests with the repository's virtual environment; no server needed.
- `scripts/curl-auth-demo.sh`
    - Against a running Faxbot with sending turned off: creates a send/read key, queues a one-page fax, reads its status and revokes the key. It sends the fax as queue-only, so Faxbot refuses it when sending is on; it never places a call. Needs `API_KEY` (an admin key) and `FAX_API_URL`.

## Send and status helpers

- `scripts/send-fax.sh "<+15551234567>" /abs/path/file.pdf|.txt`
    - Posts PDF or TXT to `/fax` with a Faxbot API key from `FAXBOT_API_KEY`, or from `API_KEY` in the repository `.env` when `FAXBOT_API_KEY` is not set. It stops with a message if neither is set. `FAX_API_URL` sets the server address. With sending on, this sends a real fax. A returned job ID means the fax was accepted, not delivered.
- `scripts/get-status.sh <job_id>`
    - Reads `/fax/{id}` with the same key and address rules and prints JSON with `jq`. Reading a job does not send or retry it.

## Received-fax helpers

Both use `API_KEY` (an admin key) and `FAX_API_URL`, create a read-only key for received faxes and revoke it when they finish.

- `scripts/inbound-smoke.sh` (`make inbound-smoke`)
    - Adds a test fax (no call), reads it with the read-only key and downloads its PDF, checking that it is a PDF.
- `scripts/inbound-watch.sh` (`make inbound-e2e`)
    - Shows the carrier trunk's status when a trunk receives, then waits for the next fax you send to one of your numbers (`WAIT_MINUTES`, default 10) and downloads its PDF.

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
