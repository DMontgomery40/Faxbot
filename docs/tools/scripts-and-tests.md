# Scripts and Tests

This page lists the helper scripts and the core API tests that check Faxbot quickly. Some helpers read values from `.env`, as noted below. Every API request needs a Faxbot API key.

On an existing installation, change server settings in [Settings](../admin-console/settings.md). Later edits to `.env` do not change them.

## In the console

**System → Developer → Scripts & checks** has tools to check Faxbot by hand. None of them sends a fax or changes a setting.

- **Add a test fax** puts a one-page fax in **Received**. It is marked as a test everywhere. It goes through owners, mailbox rules and email delivery like a real fax, but no call is made. Receiving must be on.
- **How the receiving provider reaches Faxbot** shows the address to give a provider that calls Faxbot when a fax arrives (Phaxio, Sinch, SignalWire, eFax notifications). It has a copy button and a link to the provider's guide. For a carrier trunk, it shows whether received faxes reach Faxbot. For a provider that Faxbot collects faxes from (HumbleFax), it says that there is nothing to set.
- **Fax engine** lists what Faxbot's own fax engine reports now: trunk sign-ins, the addresses it checks, calls in progress and faxes in progress. The command line has the same lists: `faxbot system diagnostics engine registrations|contacts|calls|faxes`.
- **Server checks** run read-only commands on Faxbot's server: the Python version, the document converter version and the files in the fax data folder. They work only when the installation allows it (`ENABLE_ADMIN_EXEC`). On the command line, use `faxbot system actions run <id>`.

To check whether everything works, use [Diagnostics](../admin-console/diagnostics.md).

## Sign-in and API basics

- `scripts/run-uvicorn-dev.sh`
    - Starts the API from your working tree, without Docker, on `127.0.0.1` with sending turned off. `PORT` sets the port (default 8080).
- `scripts/smoke-auth.sh`
    - Runs the API key tests with the repository's virtual environment. It needs no server.
- `scripts/curl-auth-demo.sh`
    - Needs a running Faxbot with sending turned off. It creates a send-and-read key, queues a one-page fax, reads its status and revokes the key. It sends the fax as queue-only, so Faxbot refuses it when sending is on. It never places a call. It needs `API_KEY` (an admin API key) and `FAX_API_URL`.

## Send and status helpers

- `scripts/send-fax.sh "<+15551234567>" /abs/path/file.pdf|.txt`
    - Sends a PDF or TXT file to `/fax`. It uses the Faxbot API key in `FAXBOT_API_KEY`. When `FAXBOT_API_KEY` is not set, it uses `API_KEY` from the repository's `.env`. When neither is set, it stops with a message. `FAX_API_URL` sets the server address.
    - With sending on, this sends a real fax. A returned job ID means that Faxbot accepted the fax, not that the fax was delivered.
- `scripts/get-status.sh <job_id>`
    - Reads `/fax/{id}` with the same key and address rules, and prints JSON with `jq`. Reading a job does not send it or retry it.

## Received-fax helpers

Both helpers use `API_KEY` (an admin API key) and `FAX_API_URL`. They create a read-only key for received faxes and revoke it when they finish.

- `scripts/inbound-smoke.sh` (`make inbound-smoke`)
    - Adds a test fax (no call), reads it with the read-only key and downloads its PDF. It checks that the file is a PDF.
- `scripts/inbound-watch.sh` (`make inbound-e2e`)
    - Shows the carrier trunk's status when a carrier trunk receives faxes. Then it waits for the next fax that you send to one of your numbers, and downloads its PDF. `WAIT_MINUTES` sets how long it waits (default 10).

## Environment and terminal helpers

- `scripts/load-env.sh`
    - Exports the variables in `.env` into the current shell. Most scripts source it.
- `scripts/install-terminal-deps.sh`
    - Installs the Python and console dependencies of the Admin Console's Terminal. See the Terminal guide.

## Release (maintainers)

- `scripts/release_npm.sh`
    - Publishes the Node packages (`node_mcp`, `sdks/node`) to npm. It needs `npm login` or `NPM_TOKEN`.
- `scripts/release_pypi.sh`
    - Builds the Python packages (`sdks/python`, `python_mcp`) and uploads them to PyPI. It needs `twine` sign-in.

## Node MCP scripts

These scripts run the Node MCP server (the AI assistant integration). They need a running Faxbot API and use `FAX_API_URL` and `API_KEY` from your environment. Only the stdio server uses `API_KEY`. The Streamable HTTP server uses each client's own Faxbot API key.

- `node_mcp/scripts/start-stdio.sh`
    - Starts the stdio server (`src/servers/stdio.js`). Use it for desktop assistants. It uses `filePath`, not base64.
- `node_mcp/scripts/start-http.sh`
    - Starts the Streamable HTTP server on port 3001 (`src/servers/http.js`).
- The Node package has no SSE server. For clients that support only SSE, use `python_mcp/server.py`.
- `node_mcp/scripts/test-stdio.js "<to>" <filePath>`
    - Starts the stdio server and calls the `send_fax` tool with a local file path. Example:
        - `node node_mcp/scripts/test-stdio.js "+15551234567" /abs/path/sample.pdf`
- `node_mcp/scripts/call-send-fax.js "<to>" <filePath>`
    - Calls the `send_fax` tool handler directly, without a transport, for quick local tests.

Notes

- Streamable HTTP and SSE send files as base64 inside a JSON body. The limit is 16 MB, and the base64 overhead counts toward it. REST uploads use the configured file size limit (default 10 MB).
- For desktop assistants, use stdio and `filePath`. This avoids the base64 overhead.

## API tests

The [API Tests Overview](api-tests.md) has the commands for an isolated development environment and the coverage map. The production image does not include the test suite, so the older `make test` target does not run the suite.

For checks by an operator, [sending turned off](../setup/test-mode.md) accepts real documents as held jobs. Faxbot makes no provider attempt for them. Held jobs never go out by themselves when you turn sending on, and fake callbacks cannot mark them as delivered.
