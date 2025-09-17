---
layout: default
title: Scripts and Tests
parent: Development
nav_order: 3
permalink: /development/scripts-and-tests.html
---

# Scripts and Tests

A practical catalog of helper scripts and core API tests so you can validate Faxbot quickly. These scripts auto‑load `.env` via `scripts/load-env.sh` where noted.

## Auth and API basics
- `scripts/run-uvicorn-dev.sh`
  - Starts the API from your working tree (no Docker). Accepts `PORT` (default 8080). Good for rapid iteration.
- `scripts/smoke-auth.sh`
  - Creates a venv, installs API deps, and runs a minimal auth smoke test with pytest.
- `scripts/curl-auth-demo.sh`
  - Hits a running API; mints a DB key via admin endpoint, sends a TXT/PDF fax, then fetches job status.

## Send and status helpers
- `scripts/send-fax.sh "<+15551234567>" /abs/path/file.pdf|.txt`
  - Simple wrapper to POST `/fax` with PDF or TXT (sets content‑type automatically).
- `scripts/get-status.sh <job_id>`
  - GET `/fax/{id}` for a given job; prints JSON via `jq` when available.

## Inbound helpers
- `scripts/bootstrap-inbound.sh`
  - One‑button setup: sets `INBOUND_ENABLED=true`, generates `ASTERISK_INBOUND_SECRET` if missing, ensures `REQUIRE_API_KEY=true`, restarts API (docker compose), and runs the inbound smoke.
- `scripts/inbound-internal-smoke.sh`
  - Posts a simulated internal Asterisk inbound event, lists `/inbound`, and downloads `/inbound/{id}/pdf` using a freshly minted read token.
- `scripts/e2e-inbound-sip.sh`
  - Checks health and Asterisk registration, mints an inbound read token, watches `/inbound` for a new item after you fax to your DID, and downloads the PDF when available.

## Cloud ingress (Phaxio) helper
- `scripts/setup-phaxio-tunnel.sh`
  - Starts an HTTPS tunnel (prefers cloudflared; falls back to ngrok), discovers a public URL, updates `.env` (`PUBLIC_API_URL`, `PHAXIO_CALLBACK_URL`, `FAX_BACKEND=phaxio`), and restarts the API (cloud‑only).

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
Helper scripts for the Node MCP server (AI assistant integration). Requires a running Faxbot API; inherits `FAX_API_URL` and `API_KEY` from your environment.

- `node_mcp/scripts/start-stdio.sh`
  - Launches stdio transport (`src/servers/stdio.js`). Best for desktop assistants; uses `filePath` (no base64).
- `node_mcp/scripts/start-http.sh`
  - Launches HTTP transport on port 3001 (`src/servers/http.js`).
- `node_mcp/scripts/start-sse.sh`
  - Launches SSE transport on port 3002 (`src/servers/sse.js`).
- `node_mcp/scripts/test-stdio.js "<to>" <filePath>`
  - Spawns the stdio server and calls the `send_fax` tool using a local file path. Example:
    - `node node_mcp/scripts/test-stdio.js "+15551234567" /abs/path/sample.pdf`
- `node_mcp/scripts/call-send-fax.js "<to>" <filePath>`
  - Calls the `send_fax` tool handler directly (bypasses transport) for quick local testing.

Notes
- HTTP/SSE transports require base64 JSON for files and respect a ~16 MB JSON limit; the REST API enforces a 10 MB raw file size limit.
- Prefer stdio + `filePath` to avoid base64 overhead for desktop integrations.

## API tests overview
Core test coverage lives under `api/tests/`. Run via Docker (`make test`) or locally with a venv (`pytest api/tests`).

- `test_api.py` — Health endpoint; basic `/fax` validation and TXT send path.
- `test_api_keys.py` — Admin mint/list/revoke; using minted token to send and read; revoked key rejection.
- `test_api_scopes.py` — Scope enforcement for `fax:send` vs `fax:read`.
- `test_rate_limit.py` — Per‑key RPM enforcement.
- `test_phaxio.py` — Phaxio service init, mocked send, status mapping, callback handling, PDF token endpoint behavior.
- `test_inbound_internal.py` — Internal Asterisk inbound → list → get → PDF download with scopes.
- `test_freeswitch.py` — FreeSWITCH disabled‑mode send with simulated outbound result callback.

Tips
- Set `FAX_DISABLED=true` to simulate send success without contacting providers.
- Use a temporary `FAX_DATA_DIR` per run to isolate artifacts.

