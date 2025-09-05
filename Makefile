SHELL := /bin/bash

.PHONY: up down build logs fmt test mcp-up mcp-down mcp-setup mcp-logs

up:
	docker compose up -d --build

down:
	docker compose down

logs:
	docker compose logs -f --tail=200

build:
	docker compose build

test:
	docker compose run --rm api pytest -q

# MCP-specific commands (2025 Standards)
mcp-install:
	./install.sh

mcp-setup:
	cd api && node setup-mcp.js

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

# Package management
npm-global:
	cd api && npm run install-global

homebrew-install:
	brew bundle

