# Scripts and Tests

The helper scripts and core API tests below let you check Faxbot quickly. Some helpers read values from `.env`, as noted, and every API request needs a Faxbot API key. On an existing installation, change server settings in [Settings](../admin-console/settings.md); later edits to `.env` don't change them.

## In the console

**Administration → Developer → Scripts & checks** has tools for checking Faxbot by hand. None of them sends a fax or changes a setting.

- **Add a test fax** puts a one-page fax in **Received**, marked as a test everywhere. It goes through owners, mailbox rules and email delivery just like a real fax, but no call is made. Receiving has to be on.
- **How the receiving provider reaches Faxbot** shows the address to give a provider that calls Faxbot when a fax arrives (Phaxio, Sinch, SignalWire, eFax notifications), with a copy button and a link to that provider's guide. With a carrier trunk it shows whether received faxes reach Faxbot, and for a provider Faxbot collects faxes from, such as HumbleFax, it tells you there is nothing to set.
- **Fax engine** lists what Faxbot's own fax engine reports right now: trunk sign-ins, the addresses it checks, and calls and faxes in progress. The command line has the same lists: `faxbot system diagnostics engine registrations|contacts|calls|faxes`.

To see whether everything is working, use [Diagnostics](../admin-console/diagnostics.md).

## Sign-in and API basics

- `scripts/run-uvicorn-dev.sh` starts the API from your working tree without Docker, on `127.0.0.1` with sending turned off. `PORT` sets the port (default 8080).
- `scripts/smoke-auth.sh` runs the API key tests with the repository's virtual environment. It doesn't need a server.
- `scripts/curl-auth-demo.sh` walks through the key flow against a running Faxbot with sending turned off: it creates a send-and-read key, queues a one-page fax, reads its status and revokes the key. The fax is queue-only, so Faxbot refuses it if sending is on, and the demo never places a call. It needs `API_KEY` (an admin API key) and `FAX_API_URL`.

## Send and status helpers

- `scripts/send-fax.sh "<+15551234567>" /abs/path/file.pdf|.txt` sends a PDF or TXT file to `/fax`. It uses the key in `FAXBOT_API_KEY`, or `API_KEY` from the repository's `.env` if that isn't set, and stops with a message if neither is. `FAX_API_URL` sets the server address. With sending on, this sends a real fax; a returned job ID means Faxbot accepted the fax, not that it was delivered.
- `scripts/get-status.sh <job_id>` reads `/fax/{id}` with the same key and address rules and prints the JSON with `jq`. Reading a job never sends or retries it.

## Received-fax helpers

Both helpers use `API_KEY` (an admin API key) and `FAX_API_URL`. Each creates a read-only key for received faxes and revokes it when it finishes.

- `scripts/inbound-smoke.sh` (`make inbound-smoke`) adds a test fax without a call, reads it with the read-only key, downloads its PDF and checks that the file really is a PDF.
- `scripts/inbound-watch.sh` (`make inbound-e2e`) first shows the carrier trunk's status, when a carrier trunk receives your faxes. It then waits for the next fax you send to one of your numbers and downloads its PDF. `WAIT_MINUTES` sets how long it waits (default 10).

## Environment and terminal helpers

- `scripts/load-env.sh` exports the variables in `.env` into the current shell. Most scripts source it.
- `scripts/install-terminal-deps.sh` installs the Python and console dependencies for the Admin Console's Terminal. See the Terminal guide.

## Node MCP scripts

These scripts run the Node MCP server, the AI assistant integration. They need a running Faxbot API and read `FAX_API_URL` and `API_KEY` from your environment. Only the stdio server uses `API_KEY`; the Streamable HTTP server uses each client's own Faxbot API key.

- `node_mcp/scripts/start-stdio.sh` starts the stdio server (`src/servers/stdio.js`). It suits desktop assistants and takes a `filePath` rather than base64.
- `node_mcp/scripts/start-http.sh` starts the Streamable HTTP server on port 3001 (`src/servers/http.js`).
- The Node package has no SSE server. For clients that only support SSE, use `python_mcp/server.py`.
- `node_mcp/scripts/test-stdio.js "<to>" <filePath>` starts the stdio server and calls the `send_fax` tool with a local file, for example `node node_mcp/scripts/test-stdio.js "+15551234567" /abs/path/sample.pdf`.
- `node_mcp/scripts/call-send-fax.js "<to>" <filePath>` calls the `send_fax` tool handler directly, without a transport, for quick local tests.

Streamable HTTP and SSE send files as base64 inside a JSON body limited to 16 MB, and the base64 overhead counts toward that limit. REST uploads use the configured file size limit (10 MB by default). For desktop assistants, stdio with `filePath` avoids the overhead entirely.

## API tests

The [API Tests Overview](api-tests.md) has the commands for an isolated development environment and a map of what the tests cover. From a checkout, `make test` runs them the way CI does.

For operator checks, turn [sending off](../setup/test-mode.md): Faxbot then accepts real documents as held jobs and makes no provider attempt for them. Held jobs never go out on their own when you turn sending back on, and a fake callback can't mark one as delivered.
