---
layout: default
title: Scripts and Tests (Guide)
parent: Scripts and Tests
nav_order: 1
permalink: /scripts-and-tests.html
---

# Scripts and Tests

This page lists the most useful helper scripts and tests so you can verify Faxbot quickly without hand‑assembling curl commands.

All scripts auto‑load `.env` via `scripts/load-env.sh`. Put your settings there.

## Auth and API basics
- `scripts/smoke-auth.sh`
  - Creates a local venv, installs deps, runs a minimal pytest that creates a DB key, sends a test fax, queries status, and revokes the key.
- `scripts/run-uvicorn-dev.sh`
  - Starts the API from your working tree (no Docker); accepts `PORT` (default 8080).
- `scripts/curl-auth-demo.sh`
  - Assumes a running API; mints a DB key via admin endpoint, sends a TXT/PDF fax, and fetches the job status.

## Inbound
- `scripts/bootstrap-inbound.sh`
  - One-button setup for inbound on your machine/server: sets `INBOUND_ENABLED=true`, generates `ASTERISK_INBOUND_SECRET` if missing, ensures `REQUIRE_API_KEY=true`, restarts API (docker compose), and runs the inbound smoke.
- `scripts/inbound-internal-smoke.sh`
  - Posts a simulated inbound TIFF to `/_internal/asterisk/inbound`, lists `/inbound`, and downloads `/inbound/{id}/pdf` using a freshly minted inbound read token.
- `scripts/e2e-inbound-sip.sh`
  - Checks health and Asterisk registration, mints an inbound read token, and watches `/inbound` for a new item after you fax to your DID; downloads the PDF when available.

## Cloud ingress helpers
- `scripts/setup-phaxio-tunnel.sh`
  - Starts an HTTPS tunnel (cloudflared/ngrok), discovers a public URL, updates `.env` with `PUBLIC_API_URL` and `PHAXIO_CALLBACK_URL`, and restarts the API (cloud‑only). Useful for local Phaxio testing.

## Makefile shortcuts
- `make up` / `make down` / `make logs`
- `make up-cloud` (API only; no Asterisk)
- `make test` (runs pytest in the API container)
- `make inbound-smoke` / `make inbound-e2e`
- `scripts/check-env.sh` — validates your `.env` for the selected backend and inbound/storage settings; prints what's missing and why.
- Alembic:
  - `make alembic-upgrade` (upgrade to head)
  - `make alembic-downgrade` (step down one)
  - `make alembic-revision` (info for autogenerate; use DATABASE_URL env)

## Notes
- For production, host the API as a container behind TLS; the UI can be hosted separately (e.g., Netlify) and calls the API over HTTPS.
- Use Postgres in production (`DATABASE_URL=postgresql+psycopg2://…`). SQLite remains for dev.
- Store artifacts on S3 or S3‑compatible storage with SSE‑KMS for PHI (configure `STORAGE_BACKEND`, `S3_*`).
