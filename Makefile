SHELL := /bin/bash

.PHONY: up down build logs fmt test mcp-up mcp-down mcp-setup mcp-logs mcp-sse-up mcp-sse-down mcp-sse-logs

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

test:
	docker compose run --rm api pytest -q

# Local equivalents of the CI jobs (see CONTRIBUTING.md).
# VENV can point at an existing venv, e.g. make test-local VENV=/path/to/.venv
VENV ?= .venv
PYTEST_ARGS ?=

.PHONY: venv test-local ui-build

venv:
	uv venv --python 3.11 $(VENV) && uv pip install --python $(VENV)/bin/python -r api/requirements.txt -r python_mcp/requirements.txt

# Same command and env as the test-api CI job; set FAXBOT_SCHEMA_TEST_POSTGRES_URL to include the PostgreSQL schema tests.
test-local:
	cd api && mkdir -p faxdata && FAX_DISABLED=true FAX_DATA_DIR=./faxdata DATABASE_URL='sqlite:///./test_faxbot_ci.db' FAXBOT_SCHEMA_TEST_POSTGRES_URL="$${FAXBOT_SCHEMA_TEST_POSTGRES_URL:-}" $(abspath $(VENV))/bin/python -m pytest -q $(PYTEST_ARGS)

ui-build:
	cd api/admin_ui && npm ci --no-audit --no-fund && npm run build

# T.38 loopback proof: two Faxbot Asterisk containers exchange a two-page fax,
# a stand-in phone system on a local network faxes with Faxbot both ways, and
# the UK and Australian carrier presets fax both ways as Faxbot renders them.
# Needs Docker; DOCKER_CONTEXT defaults to colima-faxbot-refresh. FAXBOT_NATIVE_IMAGE
# and FAXBOT_ROUTER_IMAGE name already-built images to use instead of building them.
DOCKER_CONTEXT ?= colima-faxbot-refresh
.PHONY: native-proof
native-proof:
	cd api && mkdir -p faxdata && FAXBOT_NATIVE_PROOF=1 FAXBOT_DOCKER_CONTEXT=$(DOCKER_CONTEXT) FAX_DISABLED=true FAX_DATA_DIR=./faxdata DATABASE_URL='sqlite:///./test_faxbot_ci.db' $(abspath $(VENV))/bin/python -m pytest -q -s -p no:cacheprovider -m native tests/test_t38_loopback.py tests/test_phone_system_loopback.py

# The faxbot command line from this checkout, for example: make cli ARGS="health"
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
inbound-smoke:
	API_KEY=$${API_KEY} ASTERISK_INBOUND_SECRET=$${ASTERISK_INBOUND_SECRET} ./scripts/inbound-internal-smoke.sh

inbound-e2e:
	API_KEY=$${API_KEY} ./scripts/e2e-inbound-sip.sh

# MCP-specific commands (2025 Standards)
mcp-install:
	./install.sh

mcp-setup:
	@echo "See docs/MCP_INTEGRATION.md for stdio/HTTP/SSE config"

mcp-up:
	docker compose --profile mcp up -d --build

mcp-http:
	cd api && npm run start:http

mcp-stdio:
	cd api && npm run start:mcp

mcp-down:
	docker compose --profile mcp down

mcp-logs:
	docker compose logs -f faxbot-mcp

mcp-sse-up:
	docker compose --profile mcp up -d --build faxbot-mcp-sse

mcp-sse-down:
	docker compose --profile mcp down

mcp-sse-logs:
	docker compose logs -f faxbot-mcp-sse

# Package management
npm-global:
	cd api && npm run install-global

homebrew-install:
	brew bundle
