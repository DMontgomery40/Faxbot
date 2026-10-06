SHELL := /bin/bash

.PHONY: up up-cloud down build logs test mcp-up mcp-down mcp-logs

up:
	docker compose up -d --build

# For cloud backend only (Phaxio) - skips Asterisk
up-cloud:
	docker compose up -d --build api

down:
	docker compose down

logs:
	docker compose logs -f --tail=200

build:
	docker compose build

# make test runs the API tests from this checkout the same way the test-api CI job does.
# The API image does not include the tests.
test: test-local

# Local equivalents of the CI jobs (see CONTRIBUTING.md).
# VENV can point at an existing venv, e.g. make test-local VENV=/path/to/.venv
VENV ?= .venv
PYTEST_ARGS ?=
# PGDB names a disposable PostgreSQL test database for the PostgreSQL schema variants; the
# server URL without a database name is read from PG_URL_FILE (one line, kept out of the repo).
PGDB ?=
PG_URL_FILE ?= $(HOME)/.config/faxbot/postgres-test-url
# The whole suite is the combined gate; it runs only with FULL_GATE=1.
FULL_GATE ?=
# Node 24 for the console checks; empty uses whatever node is on PATH.
NODE_BIN ?= $(firstword $(wildcard /opt/homebrew/opt/node@24/bin))
VITEST_ARGS ?=

.PHONY: venv test-local ui-build ui-check

venv:
	uv venv --python 3.11 $(VENV) && uv pip install --python $(VENV)/bin/python -r api/requirements.txt -r python_mcp/requirements.txt

# Same command and env as the test-api CI job. Name the files to run:
#   make test-local PYTEST_ARGS='tests/test_routing_http.py' PGDB=faxbot_test_x
# PGDB (or FAXBOT_SCHEMA_TEST_POSTGRES_URL) adds the PostgreSQL schema variants.
# The whole suite runs only as the combined gate: make test-local FULL_GATE=1
test-local:
	@if [ -z "$(strip $(PYTEST_ARGS))" ] && [ "$(FULL_GATE)" != 1 ]; then echo "Name the test files to run, for example PYTEST_ARGS='tests/test_cli.py'. The whole suite is the combined gate: add FULL_GATE=1."; exit 2; fi
	@if [ -n "$(PGDB)" ] && [ ! -s "$(PG_URL_FILE)" ]; then echo "PGDB needs the PostgreSQL server URL in $(PG_URL_FILE)."; exit 2; fi
	cd api && mkdir -p faxdata && FAX_DISABLED=true FAX_DATA_DIR=./faxdata DATABASE_URL='sqlite:///./test_faxbot_ci.db' FAXBOT_SCHEMA_TEST_POSTGRES_URL="$(if $(PGDB),$$(cat '$(PG_URL_FILE)')/$(PGDB),$${FAXBOT_SCHEMA_TEST_POSTGRES_URL:-})" $(abspath $(VENV))/bin/python -m pytest -q -p no:cacheprovider $(PYTEST_ARGS)

ui-build:
	cd api/admin_ui && npm ci --no-audit --no-fund && npm run build

# Console checks: vitest, type check and build (VITEST_ARGS narrows vitest to named files).
ui-check:
	cd api/admin_ui && export PATH="$(if $(NODE_BIN),$(NODE_BIN):)$$PATH" && { [ -d node_modules ] || npm ci --no-audit --no-fund; } && npx vitest run $(VITEST_ARGS) && npx tsc --noEmit && npm run build

# T.38 loopback proof: two Faxbot Asterisk containers exchange a two-page fax,
# a stand-in phone system on a local network faxes with Faxbot both ways, and
# the UK and Australian carrier presets fax both ways as Faxbot renders them.
# Needs Docker; DOCKER_CONTEXT defaults to colima-faxbot-refresh. FAXBOT_NATIVE_IMAGE
# and FAXBOT_ROUTER_IMAGE name already-built images to use instead of building them.
DOCKER_CONTEXT ?= colima-faxbot-refresh
.PHONY: native-proof
native-proof:
	cd api && mkdir -p faxdata && FAXBOT_NATIVE_PROOF=1 FAXBOT_DOCKER_CONTEXT=$(DOCKER_CONTEXT) FAX_DISABLED=true FAX_DATA_DIR=./faxdata DATABASE_URL='sqlite:///./test_faxbot_ci.db' $(abspath $(VENV))/bin/python -m pytest -q -s -p no:cacheprovider -m native tests/test_t38_loopback.py tests/test_phone_system_loopback.py

# SSL Fax loopback proof, through Faxbot's own API: Faxbot (API, Asterisk and the
# HylaFAX+ engine) sends to a stand-in carrier Asterisk and a peer engine on a
# private network, over SSL Fax on an audio call, across two T.38 gateways, and
# falling back to an ordinary fax when the peer's listener cannot be reached or
# names a private address. Needs Docker; never runs in CI. FAXBOT_NATIVE_IMAGE,
# FAXBOT_ENGINE_IMAGE and FAXBOT_API_IMAGE name already-built images to use.
.PHONY: sslfax-proof
sslfax-proof:
	cd api && mkdir -p faxdata && FAXBOT_SSLFAX_PROOF=1 FAXBOT_DOCKER_CONTEXT=$(DOCKER_CONTEXT) FAX_DISABLED=true FAX_DATA_DIR=./faxdata DATABASE_URL='sqlite:///./test_faxbot_ci.db' $(abspath $(VENV))/bin/python -m pytest -q -s -p no:cacheprovider -m native tests/test_sslfax_loopback.py $(PYTEST_ARGS)

# The faxbot command line from this checkout, for example: make cli ARGS="system health"
# Paths in ARGS stay relative to where make runs. See docs/operations/cli.md.
.PHONY: cli cli-docs
cli:
	PYTHONPATH=$(CURDIR)/api $(abspath $(VENV))/bin/python -m app.cli $(ARGS)

# Regenerate docs/reference/cli.md from the command definitions (checked by api/tests/test_cli.py).
cli-docs:
	cd api && $(abspath $(VENV))/bin/python -m app.cli.reference > ../docs/reference/cli.md

# Docs Autopilot proposal with Codex on your own sign-in (codex login; no API key):
#   make docs-propose BASE=<previous commit> [APPLY=1]
# Writes mkdocs-docs-llm.patch only after validation; APPLY=1 also stages it. Nothing is committed.
.PHONY: docs-propose
docs-propose:
	@test -n "$(BASE)" || { echo "Set BASE to the previous commit, for example: make docs-propose BASE=HEAD~1"; exit 2; }
	$(abspath $(VENV))/bin/python scripts/docs_ai/generate_docs_from_diff.py --base "$(BASE)" --llm codex $(if $(APPLY),--apply,)

# Docs Autopilot audit: check maintained pages against the current code (no base), a few pages per Codex call.
#   make docs-audit [PAGES='docs/setup/sip-asterisk.md docs/plugins/*.md']
# Writes mkdocs-docs-findings.md, and mkdocs-docs-llm.patch when a validated change is proposed. Nothing is committed.
.PHONY: docs-audit
docs-audit:
	$(abspath $(VENV))/bin/python scripts/docs_ai/generate_docs_from_diff.py --audit --llm codex $(if $(PAGES),--pages "$(PAGES)",)

# Alembic helpers (run locally)
alembic-upgrade:
	DATABASE_URL=$${DATABASE_URL:-sqlite:///./faxbot.db} alembic -c api/alembic.ini upgrade head

test-schema:
	@test -n "$$FAXBOT_SCHEMA_TEST_POSTGRES_URL" || (echo "Set FAXBOT_SCHEMA_TEST_POSTGRES_URL to a dedicated disposable PostgreSQL test database"; exit 1)
	python -m pytest api/tests/test_schema.py -q

alembic-downgrade:
	DATABASE_URL=$${DATABASE_URL:-sqlite:///./faxbot.db} alembic -c api/alembic.ini downgrade -1

alembic-revision:
	@echo "Use: DATABASE_URL=... alembic -c api/alembic.ini revision -m 'message' --autogenerate"

# Inbound helpers
# Add a test fax and download its PDF through the public API (no call).
inbound-smoke:
	API_KEY=$${API_KEY} ./scripts/inbound-smoke.sh

# Wait for the next real fax to arrive and download its PDF.
inbound-e2e:
	API_KEY=$${API_KEY} ./scripts/inbound-watch.sh

# MCP servers in Docker (the compose profile "mcp")
mcp-up:
	docker compose --profile mcp up -d --build

mcp-down:
	docker compose --profile mcp down

mcp-logs:
	docker compose logs -f faxbot-mcp
