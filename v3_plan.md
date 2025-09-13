# Faxbot v3 Refactor Plan — Plugins, MCP, SDKs, and Admin UI

Audience: This document is written for agents and developers, including low-parameter LLMs with a knowledge cutoff before MCP existed. It contains explicit, step-by-step instructions with no assumed knowledge. Follow the steps in order. Copy code blocks exactly. Do not improvise names, paths, or JSON keys.

Status: Draft on development branch. All work targets `development`. Short‑lived feature branches may be created only with owner approval and must merge back into `development` via PR. Do not push directly to `main`. A formal migration and release plan appears near the end.

Table of Contents
1. Glossary and Core Concepts
2. Repository Layout and Branching Rules
3. High-Level Goals of v3
4. Breaking Changes and Compatibility Strategy
5. Plugin Architecture (Design and Contracts)
6. Backend Implementation (FastAPI) — Plugin Discovery and Config APIs
7. MCP Servers (Node + Python) — Dynamic Tools from Capabilities
8. Admin UI — Add a “Plugins” Tab (step-by-step)
9. SDKs (Node and Python) — OpenAPI-driven generation and publish flows
10. Plugin Developer Guide — How to build, package, and publish plugins (npm + PyPI)
11. Plugin Registry and Search — How users find plugins in the UI
12. Security, Permissions, and Safe Defaults
13. Deployment (Docker/Compose), Config, and Profiles
14. Telemetry, Logging, and Observability
15. Testing Plan (Unit, Integration, E2E)
16. Migration Plan from v2 to v3
17. Release Plan and Milestones
18. Checklists (per area)

1) Glossary and Core Concepts
- Core: The minimal Faxbot system (API, Admin UI, MCP servers) without any specific provider baked in.
- Plugin: A package that implements a capability (e.g., outbound fax via SIP, inbound fax via webhook, auth provider). Plugins live in npm or PyPI and are installed by the operator.
- Provider Slot: A category of plugin (e.g., outbound, inbound, auth, storage) that the core can bind to at runtime using configuration.
- Capability: A named function a plugin can perform (e.g., send, get_status, list_inbound, get_inbound_pdf). Capabilities map to MCP tools and API operations.
- Manifest: A JSON-like metadata object shipped by each plugin that describes id, name, version, capabilities, config schema, and defaults.
- MCP (Model Context Protocol): A protocol for exposing tools/resources to AI assistants. We run MCP servers (Node and Python) that register tools based on the active plugins and their capabilities.
- Admin UI: The local-only web console for configuration and monitoring. It will gain a Plugins tab to manage plugins and their settings.
- Registry: A curated catalog of known plugins that the UI can search. We start with a static JSON file in the repo. Remote install is optional and off by default.

2) Repository Layout and Branching Rules
- Primary repo: DMontgomery40/Faxbot
- Existing directories (do not rename in v3 unless migration step calls for it):
  - api/ (FastAPI services, Admin UI backend endpoints)
  - api/admin_ui/ (React app for console)
  - node_mcp/ (Node MCP server)
  - python_mcp/ (Python MCP server)
  - sdks/node/ (Node client SDK)
  - sdks/python/ (Python client SDK)
  - docs/ (documentation)

Branching
- main: stable releases only. No direct pushes. Release tags (e.g., v3.x) are cut from main.
- development: integration branch for v3. PRs merge here after CI passes.
- feature/*: short‑lived per‑task branches cut from development are allowed only with owner approval and must merge back into development via PR.

3) High-Level Goals of v3
- Introduce a plugin system to avoid N×M bundles. Users install only the capabilities they need.
- Provide a Plugins tab in the Admin UI to discover, enable, and configure plugins.
- Discover plugins at runtime (Python entry points; Node npm keywords) and wire them via configuration.
- Dynamically register MCP tools based on active plugin capabilities.
- Keep Node and Python SDKs, with Python generated from OpenAPI to minimize maintenance.
- Maintain a curated plugin registry JSON to power UI search. Remote install is optional and disabled by default.

4) Breaking Changes and Compatibility Strategy
- New configuration model: v3 introduces a single resolved config that binds provider slots to plugin IDs plus settings. Older ad hoc environment variables may be deprecated.
- MCP tool names/signatures: standardized across Node and Python, generated from a shared schema. Legacy names are aliased during deprecation window.
- SDKs: Python SDK may shift to generated code; public API should remain compatible where possible. Document any changes.
Mitigation:
- Feature flag all v3 functionality behind FEATURE_V3_PLUGINS=true.
- Provide a config migration script that reads old env/config and writes the new config file.
- Keep legacy endpoints operational through v3.1 with warnings in logs.
- Staged rollout on development, then release/v3.x, finally main.

5) Plugin Architecture (Design and Contracts)
5.1 Provider Slots (initial)
- outbound: sending faxes (capabilities: send, get_status)
- inbound: receiving/lists (capabilities: list_inbound, get_inbound_pdf)
- auth: authentication/OIDC strategies (capabilities vary, e.g., validate_token)
- storage: store/retrieve artifacts (optional; capabilities: put, get)

5.2 Plugin Manifest (common fields)
All plugins must expose a manifest with these fields:
{
  id: string (lowercase, [a-z0-9._-]),
  name: string,
  version: string (semver),
  description: string,
  author?: string,
  homepage?: string (URL),
  license?: string,
  platform: "python" | "node",
  categories: string[] (e.g., ["outbound"]),
  capabilities: string[] (e.g., ["send", "get_status"]),
  config_schema: JSON Schema object for settings,
  defaults: object with default settings
}

5.3 Python Plugin Contract
- Packaging: PyPI package named faxbot_plugin_<shortname> (e.g., faxbot_plugin_sip)
- Entry point group: faxbot.plugins
- Entry point value: a class that implements Plugin (see api/plugins/base.py)
- Required files:
  - pyproject.toml with entry points
  - package module exposing a class with methods manifest(), validate_config(cfg), start(cfg), stop()
- Example pyproject.toml snippet:
[project.entry-points]
"faxbot.plugins" = {
  "faxbot_plugin_sip" = "faxbot_plugin_sip:SipPlugin"
}

5.4 Node Plugin Contract
- Packaging: npm package named one of:
  - @faxbot/plugin-<shortname> (preferred scoped), or
  - faxbot-plugin-<shortname>
- package.json MUST include:
  - "keywords": ["faxbot-plugin"],
  - "faxbot": { "manifest": { ...same fields as above... } }
- Module export: optionally export getManifest() for runtime validation.

5.5 Capabilities → MCP Tool Mapping
- send → tool name: send_fax
- get_status → get_fax_status
- list_inbound → list_inbound
- get_inbound_pdf → get_inbound_pdf
Rules:
- Tool names are stable and lowercase with dots. No spaces.
- Input/Output schemas defined in docs/mcp_tools.schema.json (to be added).

5.6 Config Resolution
- Single file: config/faxbot.config.json (JSON) or YAML variant.
- Structure:
{
  "version": 1,
  "providers": {
    "outbound": { "plugin": "faxbot_plugin_sip", "enabled": true, "settings": { /* plugin-specific */ } },
    "inbound": { "plugin": "faxbot_plugin_inboundwebhook", "enabled": false, "settings": {} },
    "auth": { "plugin": "faxbot_plugin_oidc", "enabled": true, "settings": {} }
  }
}
- The Admin UI writes and reads this via Backend APIs.

6) Backend Implementation (FastAPI) — Plugin Discovery and Config APIs
6.1 Files to add under api/
- api/plugins/base.py — Plugin + Manifest dataclasses and interface.
- api/plugins/registry.py — discovery using importlib.metadata.entry_points(group="faxbot.plugins").
- api/routes/plugins.py — FastAPI router with endpoints.
- api/plugins/manifest.schema.json — JSON Schema for manifest validation.
- api/services/config_store.py — read/write config file; provide get_resolved_config().

6.2 Endpoints
- GET /plugins → list installed plugins, their manifests, and current enabled/config values.
- GET /plugins/{id}/config → return enabled + settings.
- PUT /plugins/{id}/config → validate via JSON Schema then persist.
- GET /plugin-registry → return curated registry JSON (see section 11).
Optional (disabled by default behind FEATURE_PLUGIN_INSTALL=false):
- POST /plugins/install { source: "npm"|"pypi", name: string, version?: string }
- POST /plugins/uninstall { source, name }

6.3 Discovery Rules
- Python: use entry_points(group="faxbot.plugins"). Each entry must load to a class with manifest().
- Node: discovery is indirect (admin lists plugins from registry). Runtime Node MCP discovery reads config only; Node plugin discovery in API is not required initially.

6.4 Persistence
- Store config in config/faxbot.config.json on disk by default. Support env FAXBOT_CONFIG_PATH to override.
- The store must be atomic (write to temp file then rename). Create parent directories if missing.

6.5 Validation
- Use jsonschema to validate settings against manifest.config_schema.
- If validation fails, return 400 with details.

6.6 Feature Flags
- FEATURE_V3_PLUGINS=true enables:
  - /plugins and /plugin-registry endpoints
  - Admin UI Plugins tab

7) MCP Servers (Node + Python) — Dynamic Tools from Capabilities
7.1 Shared Principles
- Both servers read the resolved config produced by api/services/config_store.get_resolved_config(). If running out-of-process, read from a shared config file path provided via env FAXBOT_CONFIG_PATH.
- Tools are registered only if the corresponding provider slot is enabled AND the plugin reports that capability.

7.2 Tool Names and Stability
- Use the mapping defined in 5.5. Do not introduce server-specific names. Keep parity.
- Input/Output schemas pulled from docs/mcp_tools.schema.json (add this file in a later task).

7.3 Node MCP Changes (node_mcp/)
- Add a config loader that reads JSON at startup.
- For each active provider slot, register tools accordingly.
- Example: if outbound.send exists, register send_fax and wire to plugin adapter.

7.4 Python MCP Changes (python_mcp/)
- Mirror Node logic. Use pydantic models for request/response schemas.

8) Admin UI — Add a “Plugins” Tab (step-by-step)
8.1 Prereqs
- Ensure api/routes/plugins.py is mounted in the FastAPI app.
- Ensure CORS permits the Admin UI origin.

8.2 File Changes (paths are relative to api/admin_ui/)
- src/api/plugins.ts: add fetchPlugins, getPluginConfig, putPluginConfig, fetchPluginRegistry.
- src/pages/Plugins.tsx: the page component described below.
- src/App.tsx: add a React Router route to /plugins.
- src/components/Sidebar.tsx (or equivalent): add a menu item “Plugins” linking to /plugins.

8.3 Install UI Dependencies
- npm i @rjsf/mui @rjsf/validator-ajv8 @mui/material @emotion/react @emotion/styled

8.4 Implement Plugins.tsx
- Fetch GET /plugins and render cards for each plugin with:
  - Enabled toggle (Switch)
  - Config form rendered from manifest.config_schema using react-jsonschema-form
  - Save button persists via PUT /plugins/{id}/config
- Add a search box powered by GET /plugin-registry to discover available plugins by keyword/tag; show “Install” buttons if FEATURE_PLUGIN_INSTALL=true, otherwise show “How to install” instructions.

8.5 UX Rules
- Disabled plugins’ forms are readonly.
- Show validation errors inline.
- Show plugin version and platform badge (python/node).
- Persist immediately on toggle, and show a toast on success/failure.

9) SDKs (Node and Python) — OpenAPI-driven generation and publish flows
9.1 OpenAPI as Source of Truth
- Commit api/openapi.yaml (exported from FastAPI or hand-authored).

9.2 Codegen Workflow
- Add .github/workflows/sdks-autogen.yml that regenerates clients when api/openapi.yaml changes or on manual dispatch.
- Node: generate to sdks/node/src/gen using openapi-typescript-codegen.
- Python: generate to sdks/python/faxbot_client using openapi-python-client.

9.3 Publishing
- Node SDK: publish on GitHub Release (existing workflow). Ensure version bump in sdks/node/package.json.
- Python SDK: publish on GitHub Release (existing workflow). Ensure version bump in sdks/python/setup.py or pyproject.toml.

10) Plugin Developer Guide — How to build, package, and publish plugins
10.1 Naming
- Python: faxbot_plugin_<shortname>
- Node: @faxbot/plugin-<shortname> (preferred) or faxbot-plugin-<shortname>

10.2 Required Metadata
- Capabilities must be accurate. If you claim send but do not implement it, validation will fail.
- Provide a JSON Schema config for your settings with types, defaults, and descriptions.

10.3 Python Example (minimal)
- pyproject.toml includes entry point group faxbot.plugins.
- Module exposes class SipPlugin with methods manifest(), start(), stop().

10.4 Node Example (minimal)
- package.json includes keywords ["faxbot-plugin"].
- package.json contains a faxbot.manifest object.

10.5 Publishing and Versioning
- Use SemVer. Breaking changes to your plugin’s config schema or behavior require a major version bump.
- After publishing, submit a PR to Faxbot’s plugin registry file (docs/plugin-registry.json) to make your plugin discoverable in the Admin UI search.

11) Plugin Registry and Search — How users find plugins in the UI
11.1 Registry File
- Location: docs/plugin-registry.json in the Faxbot repo.
- Structure: array of entries with fields: id, name, description, source ("npm"|"pypi"), package_name, keywords, homepage, version, categories, capabilities.
- The Admin UI fetches GET /plugin-registry (the backend serves the file contents).

11.2 Submission Process for Developers
- Fork Faxbot repo → edit docs/plugin-registry.json → add your plugin entry → open a PR.
- CI validates schema. On merge, users will see your plugin in the UI.

11.3 Optional Remote Search (later)
- npm search: query by keyword faxbot-plugin.
- PyPI search is limited; prefer the curated registry for reliability.

12) Security, Permissions, and Safe Defaults
- Remote install (pip/npm) is disabled by default. Admin UI shows instructions instead of an Install button unless FEATURE_PLUGIN_INSTALL=true.
- Maintain an allowlist (docs/plugin-allowlist.json). Only plugins on the allowlist can be installed via the UI, even if registry lists more.
- Never run arbitrary shell without explicit user confirmation.
- Plugins should not receive secrets they don’t need. Config forms must separate credentials and mask sensitive fields.

13) Deployment (Docker/Compose), Config, and Profiles
- Create config/faxbot.config.json and mount it into containers via volume.
- Add compose profiles for common combinations (stdio mcp, http mcp, sse+oidc). Profiles select env vars and command args.
- Document FAXBOT_CONFIG_PATH env to point to config file.

14) Telemetry, Logging, and Observability
- Add structured logs for plugin discovery, validation, and MCP tool registration.
- Optional: emit metrics for active plugins and tool calls.

15) Testing Plan (Unit, Integration, E2E)
- Unit: jsonschema validation, config store, registry discovery.
- Integration: spin up API + sample plugin; verify /plugins list, PUT config succeeds, MCP registers tools.
- E2E: Cypress (or Playwright) tests for Admin UI Plugins tab: enable/disable, save settings, warnings.

16) Migration Plan from v2 to v3
- Phase 0 (behind flag): implement v3 features hidden behind FEATURE_V3_PLUGINS.
- Phase 1 (dual): provide migration script that converts old env/config to config/faxbot.config.json. Log warnings on legacy env usage.
- Phase 2 (default): enable v3 by default; keep legacy aliases for one minor release.
- Phase 3 (remove): remove legacy aliases; update docs.

17) Release Plan and Milestones
- M1: Backend plugin APIs and config store (feature/plugins-backend-api)
- M2: Admin UI Plugins tab (feature/plugins-admin-ui)
- M3: Node MCP dynamic tools (feature/mcp-node-dynamic)
- M4: Python MCP dynamic tools (feature/mcp-python-dynamic)
- M5: SDK autogen workflow + committed OpenAPI (feature/sdks-autogen)
- M6: Docs + plugin registry + sample plugins (feature/plugins-samples)
- M7: Migration script + dual-mode release branch (release/v3.0.0-rc)

18) Checklists (copy into PR descriptions)
Backend
- [ ] api/plugins/base.py, registry.py, routes/plugins.py added and tested
- [ ] jsonschema validation wired
- [ ] config store reads/writes config/faxbot.config.json atomically

Admin UI
- [ ] Plugins tab route exists and appears in sidebar
- [ ] RJSF renders form from config_schema; Save persists correctly
- [ ] Registry search visible; install button hidden unless FEATURE_PLUGIN_INSTALL

MCP
- [ ] Node + Python servers compute tools from capabilities
- [ ] Tool names match mapping; parity maintained

SDKs
- [ ] OpenAPI committed; autogen workflow working
- [ ] Node published; Python published; versions documented

Security
- [ ] Remote install disabled by default; allowlist enforced if enabled
- [ ] Secrets masked in UI forms

Docs
- [ ] docs/plugin-registry.json created and schema-validated
- [ ] Developer guide for plugin authors added in docs/plugins-dev.md

Appendix A — Minimal Reference Implementations (to be added in subsequent PRs)
- api/plugins/base.py (Python interface)
- api/plugins/registry.py (discovery)
- api/routes/plugins.py (REST endpoints)
- admin_ui src/pages/Plugins.tsx (React page)
- node_mcp config loader and dynamic tool registrar
- python_mcp config loader and dynamic tool registrar

Important: Keep all new files and features behind FEATURE_V3_PLUGINS until M7. Document all config keys in docs/CONFIG_V3.md.

---

# Part II — Critical Evaluation and Comprehensive Overhaul Plan (Aligned to AGENTS.md)

Audience: Junior developers and low-parameter LLM agents. Copy commands exactly. Do not invent names, paths, or keys. Follow steps in order. All work must respect Admin Console First, Backend Isolation, HIPAA defaults, and branch policy described in `AGENTS.md`.

Status: This section supersedes and extends sections 3–18 above with deeper, prescriptive guidance, risk controls, and implementation scaffolding to achieve a plugin-based architecture, OpenAPI-first SDKs, Node+Python MCP parity, and an Admin UI plugin manager akin to Scrypted’s UX.

Guardrails from AGENTS.md (must-follow):
- Admin Console First: Every new capability must be surfaced and operable in the Admin UI with helper text and “Learn more” links to `docs/`.
- Backend Isolation: No mixed provider instructions anywhere (Phaxio vs SIP vs Sinch). UI and docs must show only relevant provider guidance.
- HIPAA Defaults: Secure-by-default for PHI users. Non-PHI profiles may reduce friction but must be explicit.
- Authentication Layers: MCP auth, API keys, backend creds remain separated. Never log secrets.
- Branch Policy: Long-lived branches are `main`, `development`, `docs-jekyll-site`. Feature work merges into `development`. Avoid new long-lived branches.

Table of Contents (Part II)
1. Design Goals and Anti-Goals
2. Architecture Overview (Plugins + Core) — Node and Python
3. Plugin Contracts and Lifecycles (Python-first, Node-preferred where applicable)
4. OpenAPI-First Standardization and SDK Codegen (Node + Python)
5. Admin UI Plugin Manager — UX, APIs, Permissions, and Safety
6. Security Model for Plugins — Signed Manifests, Allowlist, and Isolation
7. Configuration Model — Single Resolved Config and Profiles
8. Capability Schema and MCP Tool Mappings (Parity Guarantees)
9. Migration Strategy (v2 → v3) with Safe Rollback
10. Testing Matrix and CI/CD Enhancements (Core + Plugins + UI + MCP)
11. Observability and Auditability for PHI-safe Operations
12. Performance, Limits, and Resource Planning
13. Sample Plugins (minimal, instructive, end-to-end)
14. Documentation Plan and Admin Console “Learn more” Links
15. Delivery Milestones, Checklists, and Ready-to-Paste Commands

## 1) Design Goals and Anti-Goals

Goals
- Reduce dependency coupling across MCPs, SDKs, and backends via a plugin-based model that cleanly separates provider-specific code from Faxbot core.
- Maintain identical MCP tool names and request/response schemas across Node and Python servers.
- Make OpenAPI the single source of truth for REST semantics and SDK generation.
- Provide a first-class Admin UI Plugins tab that enables discovery, enable/disable, configuration, and (optionally) installation under strict security controls.
- Keep HIPAA-safe defaults and backend isolation everywhere, including in UI helper copy and docs links.

Anti-Goals
- Do not move core REST responsibilities into MCPs or SDKs. SDKs and MCPs must call the REST API; backends integrate via plugins into the API service.
- Do not enable arbitrary remote code execution. Plugin installation is disabled by default and constrained by allowlists and signature verification when enabled.
- Do not mix backend guidance in UI or docs. Each provider path remains isolated.

Key Changes Compared to Part I
- Strengthened security requirements for plugin manifests and installs (signed registry entries, allowlist enforcement, offline-first installs).
- OpenAPI-first pipeline mandated for SDKs, admin UI API clients, and contract tests.
- Explicit lifecycles for Python plugins that execute in the FastAPI process for backend functions; Node plugins used for MCP-side extensions and UI tooling. Cross-runtime execution bridges are optional and deferred.
- Branching text aligned to AGENTS.md: feature work merges into `development`; no additional long-lived branches.

## 2) Architecture Overview (Plugins + Core) — Node and Python

High-Level Components
- Core API (FastAPI, Python):
  - Hosts REST endpoints and executes provider logic through Python plugins for provider slots (`outbound`, `inbound`, `storage`, `auth`).
  - Reads a single resolved config from `config/faxbot.config.json` (override via `FAXBOT_CONFIG_PATH`).
  - Serves Admin UI static assets and `/plugins` management endpoints.
- MCP Servers (Node and Python):
  - Read the same resolved config.
  - Dynamically register MCP tools based on capabilities exposed by active provider slots.
  - Never contain provider logic; they forward to API endpoints.
- Admin UI (React/Vite):
  - Gains a Plugins tab: discovery, enable/disable, config forms via JSON Schema, and optional install UI (disabled by default).

Runtimes and Responsibilities
- Python plugins: Execute inside the API process for backend-critical actions (send fax, status, inbound webhooks). Reason: minimize IPC overhead, keep transactional integrity, reuse existing FastAPI/file pipeline, and simplify PHI controls.
- Node plugins: Provide MCP-side tooling enhancements and optional admin UI tooling (e.g., linting configs) but do not implement backend transmission logic. Future consideration: Node plugin bridges are deferred to a later milestone due to security surface area.

Inter-Process Boundaries
- Only the API writes to database/storage and calls transport providers (Phaxio/Sinch or Asterisk AMI) via plugins.
- MCP servers call API over HTTP (localhost/container network). SDKs do the same.

## 3) Plugin Contracts and Lifecycles

Provider Slots (unchanged, clarified)
- outbound: capabilities [send, get_status]
- inbound: capabilities [list_inbound, get_inbound_pdf, (cloud callbacks are HTTP endpoints in core that delegate to plugin handlers)]
- storage (optional): capabilities [put, get] for artifact storage backends
- auth (optional): capabilities vary (e.g., validate_token)

Common Manifest (unchanged, elaborated)
```json
{
  "id": "faxbot_plugin_sip",
  "name": "Faxbot SIP Outbound",
  "version": "1.0.0",
  "description": "Outbound fax via Asterisk/SIP with T.38",
  "author": "Faxbot Team",
  "homepage": "https://example.com",
  "license": "Apache-2.0",
  "platform": "python",
  "categories": ["outbound"],
  "capabilities": ["send", "get_status"],
  "config_schema": {"type": "object", "properties": {"ami_host": {"type": "string"}}, "required": ["ami_host"]},
  "defaults": {"ami_host": "asterisk"}
}
```

UI metadata (new, for Scrypted-like UX)
- Extend manifest with optional `ui` block:
```json
{
  "ui": {
    "hints": [
      "Use strong AMI credentials; never expose 5038 publicly.",
      "Restrict UDPTL ports to trunk provider IPs."
    ],
    "links": [
      { "title": "Faxbot SIP Setup", "url": "https://docs.faxbot.dev/backends/sip" },
      { "title": "Asterisk AMI Security", "url": "https://wiki.asterisk.org/wiki/display/AST/Manager+Configuration" }
    ],
    "uiSchema": {
      "ami_password": { "ui:widget": "password" },
      "sip_password": { "ui:widget": "password" }
    }
  }
}
```
Rules:
- Hints are concise, actionable, non-PHI. Links must be to approved domains (Faxbot docs or vendor docs relevant to the plugin’s backend).
- `uiSchema` guides Admin UI rendering (password widgets, placeholders, descriptions). Unknown keys are ignored by the UI.

Python Plugin Interface (expanded)
- Packaging: `faxbot_plugin_<shortname>` on PyPI.
- Entry points: `faxbot.plugins` using `importlib.metadata` discovery.
- Required class methods and signatures:
  - `manifest() -> dict`
  - `validate_config(config: dict) -> None` (raise `ValueError` with specific messages)
  - `start(config: dict, deps: PluginDeps) -> None`
  - `stop() -> None`
  - For outbound: `send(to_number: str, file_path: str) -> SendResult`
  - For outbound: `get_status(job_id: str) -> StatusResult`
  - For inbound: `list_inbound(...) -> InboundListResult`, `get_inbound_pdf(inbound_id: str) -> str|bytes`
  - Optional hooks: `on_health_check() -> HealthReport`

Node Plugin Interface (MCP-side)
- Packaging: `@faxbot/plugin-<shortname>` or `faxbot-plugin-<shortname>` on npm.
- Manifest location: `package.json.faxbot.manifest`.
- Optional export: `getManifest()` for runtime validation.
- Node plugins must NOT perform backend transmission or store PHI. They may:
  - Add MCP helper tools that compose REST API calls
  - Provide UI helper schemas or validators for Admin UI (non-sensitive)

Lifecycle Rules
- Plugins are loaded at API startup via discovery, then gated by the resolved config (`enabled: true/false`).
- Config changes via Admin UI trigger a controlled reload sequence:
  - Validate new config against plugin schema
  - Call `stop()` on affected plugin(s)
  - Recreate plugin instance(s) with `start()` and new config
- Fail-safe: if plugin `start()` raises, revert to last known-good config and surface actionable UI error.

## 4) OpenAPI-First Standardization and SDK Codegen

Source of Truth
- Commit `api/openapi.yaml`. Keep it exhaustive and precise. Generate both SDKs and Admin UI client types from this spec.

Required Artifacts
- `api/openapi.yaml` (hand-authored or exported from FastAPI routers with explicit models)
- Contract tests that ensure server responses conform to spec

Codegen Tools (minimal deps)
- Node SDK: `openapi-typescript-codegen`
- Python SDK: `openapi-python-client`
- Admin UI client types: `openapi-typescript`

Ready-to-paste commands (from repository root)
```bash
# Generate Node SDK (writes to sdks/node/src/gen)
npx openapi-typescript-codegen --input api/openapi.yaml --output sdks/node/src/gen --client axios --useOptions

# Generate Python SDK (writes to sdks/python/faxbot_client)
openapi-python-client generate --path api/openapi.yaml --meta none --custom-template-path sdks/python/templates --output-path sdks/python

# Generate Admin UI client types (writes to api/admin_ui/src/api/types.ts)
npx openapi-typescript api/openapi.yaml -o api/admin_ui/src/api/types.ts
```

Policy
- Any REST change must update `openapi.yaml` first. CI blocks merges when the spec and server code diverge.

## 5) Admin UI Plugin Manager — UX, APIs, Permissions, Safety

UX Requirements (Admin Console First)
- New Sidebar item `Plugins` leading to `/plugins` page.
- Cards per discovered plugin with:
  - Enabled switch (with immediate persist + toast)
  - Config form rendered via JSON Schema (read-only when disabled)
  - Version and platform badge
  - Helper text and “Learn more” links to provider-specific docs
- Registry search box shows curated results. If install is disabled, show instructions to install manually.

Backend Endpoints (must exist when `FEATURE_V3_PLUGINS=true`)
- `GET /plugins` → manifests + current config state
- `GET /plugins/{id}/config` → settings + enabled
- `PUT /plugins/{id}/config` → validate and persist (atomic write)
- `GET /plugin-registry` → serves curated registry JSON from `docs/plugin-registry.json`
- Optional when `FEATURE_PLUGIN_INSTALL=true`:
  - `POST /plugins/install` `{ source, name, version? }` (allowlist + signature verification)
  - `POST /plugins/uninstall` `{ source, name }`

Permissions
- Only Admin keys with `keys:manage` may install/uninstall plugins.
- Configure per-key RPM for plugin list/get operations as in AGENTS.md for inbound scopes.

Safety
- Install disabled by default. When enabled, enforce allowlist + checksum/signature verification.
- Never invoke shell without explicit confirmation and dry-run output in UI.

## 6) Security Model for Plugins

Registry and Allowlist
- `docs/plugin-registry.json`: public catalog consumed by UI.
- `docs/plugin-allowlist.json`: strict subset permitted for install via Admin UI.

Signature and Integrity
- Each registry entry contains `dist.sha256` and (optionally) `dist.signature` with a public key pinned in the codebase.
- Installer verifies checksum before activating plugin.

Isolation and Secrets Hygiene
- Plugins receive only the settings they require. Secrets are masked in UI and redacted in logs.
- Node plugins cannot access PHI content by design.

## 7) Configuration Model — Single Resolved Config and Profiles

Location
- Default: `config/faxbot.config.json`
- Override: `FAXBOT_CONFIG_PATH`

Structure (reaffirmed)
```json
{
  "version": 1,
  "providers": {
    "outbound": { "plugin": "faxbot_plugin_phaxio", "enabled": true, "settings": { "api_key": "..." } },
    "inbound": { "plugin": "faxbot_plugin_phaxio_inbound", "enabled": false, "settings": {} },
    "auth": { "plugin": "faxbot_plugin_oidc", "enabled": false, "settings": {} },
    "storage": { "plugin": "faxbot_plugin_s3", "enabled": false, "settings": { "bucket": "..." } }
  }
}
```

Profiles
- `profile=hipaa`: requires HTTPS, signature verification, audit log, storage encryption
- `profile=dev`: relaxed HTTPS, audit log disabled, MCP OAuth optional

## 8) Capability Schema and MCP Tool Mappings (Parity)

Stable tool names
- `send_fax`
- `get_fax_status`
- `list_inbound`
- `get_inbound_pdf`

Schemas
- Define canonical request/response JSON Schemas in `docs/mcp_tools.schema.json`.
- MCP servers in Node and Python import the same schema definitions and validate inputs. For stdio, prefer `filePath` to avoid base64; for HTTP/SSE, enforce JSON size limits (16 MB) and REST raw file limit (10 MB).

## 9) Migration Strategy (v2 → v3) with Rollback

Phases (revised to align with AGENTS.md)
- Phase 0: Implement behind `FEATURE_V3_PLUGINS=true` (default off). No user-visible behavior changes.
- Phase 1: Dual mode. Provide `scripts/migrate-config-v3.py` that reads env/legacy config and writes `faxbot.config.json`. Log deprecation warnings.
- Phase 2: Default on. Legacy env mappings still supported as aliases, with warnings.
- Phase 3: Remove aliases and update docs.

Rollback
- Keep a backup of the last known-good `faxbot.config.json.bak` (next to the config path) and a `plugins.last-ok.json` snapshot of active manifests.
- If startup validation fails, automatically revert and surface UI banner with remediation links.

## 10) Testing Matrix and CI/CD Enhancements

Unit
- Manifest validation, jsonschema config validation, config store atomic writes.

Integration
- API + sample plugin: `/plugins` listing, `PUT` config, outbound send via plugin stub.
- MCP registration parity assertions across Node/Python against the capability schema.

E2E
- Admin UI Plugins tab: enable/disable, validation errors, search/registry load, install (when enabled).

CI Gates
- OpenAPI spec lint + drift detection vs server code.
- Security: block install attempts for non-allowlisted plugins when the feature flag is off.

## 11) Observability and Auditability

Logging
- Structured logs for plugin discovery, validation errors, lifecycle events (`start`, `stop`), MCP tool registration, and config changes.

Audit Hooks (HIPAA)
- Record who enabled/disabled a plugin, when, and what changed. Do not store secrets in audit entries.

## 12) Performance and Limits

- MCP HTTP/SSE JSON max remains 16 MB; REST raw file limit 10 MB (reinforce UI pre-flight checks).
- Plugin startup time budget: < 2s per plugin (warn > 2s, fail > 10s).

## 13) Sample Plugins (Minimal, End-to-End)

Provide the following sample plugins in a dedicated `samples/` directory and publish to test registries:
- `faxbot_plugin_phaxio` (python, outbound)
- `faxbot_plugin_sip` (python, outbound)
- `faxbot_plugin_phaxio_inbound` (python, inbound)
- `@faxbot/plugin-helper-devtools` (node, MCP helper only)

Each sample includes:
- `manifest()` with JSON Schema config and defaults
- unit tests for `validate_config`
- integration tests using the API harness

## 14) Documentation Plan and Admin Console Links

Add or update docs:
- `docs/PLUGINS_OVERVIEW.md` — concepts, provider slots, manifests
- `docs/PLUGIN_SECURITY.md` — allowlist, signatures, rollback
- `docs/CONFIG_V3.md` — resolved config, profiles, env override
- `docs/plugin-registry.json` — curated registry
- `docs/plugin-allowlist.json` — install allowlist

Admin UI “Learn more” links must point to the above pages and to backend-specific setup pages without mixing providers.

## 15) Delivery Milestones and Checklists (Revised)

M1 — Backend plugin APIs and config store (development)
- Files: `api/app/plugins/base.py`, `api/app/plugins/registry.py`, `api/app/routes/plugins.py`, `api/app/services/config_store.py`, `api/app/plugins/manifest.schema.json`
- Endpoints: `/plugins`, `/plugins/{id}/config`, `/plugin-registry`
- Atomic config store with backup and revert

M2 — Admin UI Plugins Tab (development)
- Files: `api/admin_ui/src/pages/Plugins.tsx`, `api/admin_ui/src/api/plugins.ts`, route + sidebar integration
- Dependencies: `@rjsf/mui`, `@rjsf/validator-ajv8`, `@mui/material`
- UX: enable/disable with toasts, JSON Schema form, helper text, docs links

M3 — MCP Dynamic Tools (Node) (development)
- Config loader consuming `faxbot.config.json`
- Tool registration from capability schema
- Contract tests to ensure tool parity with Python MCP

M4 — MCP Dynamic Tools (Python) (development)
- Mirror Node logic with pydantic models

M5 — OpenAPI-first SDKs and Admin UI client types (development)
- Commit `api/openapi.yaml` and add codegen workflows
- Regenerate SDKs on spec change / manual dispatch

M6 — Security hardening for plugin installs (development)
- Add allowlist + checksum verification
- Optional signature verification with pinned public key

M7 — Migration tooling and dual-mode release (release/v3.0.0-rc)
- `scripts/migrate-config-v3.py`
- dual-mode flags and deprecation warnings

Junior-Friendly Ready-to-Paste Commands (examples)
```bash
# 1) Create config directory and empty config file
mkdir -p config && printf '{"version":1,"providers":{}}' > config/faxbot.config.json

# 2) Start API in dev mode on port 8080
./scripts/run-uvicorn-dev.sh

# 3) Generate Admin UI API types from OpenAPI
npx openapi-typescript api/openapi.yaml -o api/admin_ui/src/api/types.ts

# 4) Install Admin UI plugin form dependencies
cd api/admin_ui && npm i @rjsf/mui @rjsf/validator-ajv8 @mui/material @emotion/react @emotion/styled

# 5) Lint OpenAPI and fail on drift (example task)
echo "(add your chosen linter command here, e.g., redocly or spectral)"
```

Checklists (add to PRs)
- Backend
  - [ ] Plugin base + registry + routes present and tested
  - [ ] Config store atomic writes + backups verified
  - [ ] OpenAPI updated for new endpoints
- Admin UI
  - [ ] Plugins tab visible and responsive
  - [ ] JSON Schema forms render and validate
  - [ ] Docs links contextually correct and backend-isolated
- MCP
  - [ ] Node and Python register identical tools from capability schema
  - [ ] Tool schemas validated with shared definitions
- Security
  - [ ] Allowlist enforced; install disabled by default
  - [ ] Secrets masked; logs PHI-safe
- Testing
  - [ ] Unit, integration, E2E added and pass in CI

Notes on Removed/Adjusted Items
- Removed recommendation to rely on Node plugin discovery in API; API executes Python plugins for backend-critical paths. Node plugin runtime is reserved for MCP/UI helpers to reduce surface area.
- Clarified branch policy to align exactly with `AGENTS.md` (no additional long-lived branches; work targets `development`).
- Deferred cross-runtime (Node-in-API) execution bridges to future exploration after v3.0 due to security and complexity risks.

This Part II section is normative and should be treated as the canonical v3 execution plan alongside Part I. When conflicts arise, prefer the stricter security and UI-parity guidance here.

---

# Part III — Implementation Playbooks (Copy-Paste Friendly)

Important: Implement in the order shown. Keep all work behind `FEATURE_V3_PLUGINS=true` until M7. Use `development` branch for PR merges.

## A) Backend (FastAPI) — Plugins Foundation

Target files to create under `api/app/`:
- `plugins/base.py`
- `plugins/registry.py`
- `plugins/manifest.schema.json`
- `services/config_store.py`
- `routes/plugins.py`

### A.1 Create `plugins/base.py`
Purpose: Define plugin interfaces, data classes, and typed results for capabilities.

Do this:
1) Create file
```bash
mkdir -p api/app/plugins
touch api/app/plugins/base.py
```
2) Add class and type outlines (example signatures; implement fully in code):
```python
from dataclasses import dataclass
from typing import Optional, Dict, Any, List

@dataclass
class SendResult:
    job_id: str
    backend: str
    provider_sid: Optional[str]

@dataclass
class StatusResult:
    job_id: str
    status: str  # queued|in_progress|SUCCESS|FAILED
    pages: Optional[int]
    error: Optional[str]

class PluginDeps:
    def __init__(self, logger, storage, db):
        self.logger = logger
        self.storage = storage
        self.db = db

class Plugin:
    def manifest(self) -> Dict[str, Any]:
        raise NotImplementedError

    def validate_config(self, config: Dict[str, Any]) -> None:
        raise NotImplementedError

    def start(self, config: Dict[str, Any], deps: PluginDeps) -> None:
        raise NotImplementedError

    def stop(self) -> None:
        raise NotImplementedError

class OutboundPlugin(Plugin):
    def send(self, to_number: str, file_path: str) -> SendResult:
        raise NotImplementedError

    def get_status(self, job_id: str) -> StatusResult:
        raise NotImplementedError

class InboundPlugin(Plugin):
    def list_inbound(self, **kwargs) -> List[Dict[str, Any]]:
        raise NotImplementedError

    def get_inbound_pdf(self, inbound_id: str) -> bytes:
        raise NotImplementedError
```

### A.2 Create `plugins/manifest.schema.json`
Purpose: Validate plugin manifests.

```bash
cat > api/app/plugins/manifest.schema.json << 'JSON'
{
  "$schema": "http://json-schema.org/draft-07/schema#",
  "type": "object",
  "required": ["id", "name", "version", "platform", "categories", "capabilities", "config_schema", "defaults"],
  "properties": {
    "id": {"type": "string", "pattern": "^[a-z0-9._-]+$"},
    "name": {"type": "string"},
    "version": {"type": "string"},
    "description": {"type": "string"},
    "author": {"type": "string"},
    "homepage": {"type": "string"},
    "license": {"type": "string"},
    "platform": {"type": "string", "enum": ["python", "node"]},
    "categories": {"type": "array", "items": {"type": "string"}},
    "capabilities": {"type": "array", "items": {"type": "string"}},
    "config_schema": {"type": "object"},
    "defaults": {"type": "object"}
  }
}
JSON
```

### A.3 Create `services/config_store.py`
Purpose: Read/write `config/faxbot.config.json` atomically and provide `get_resolved_config()`.

```bash
mkdir -p api/app/services
touch api/app/services/config_store.py
```
Implementation requirements:
- Env override: `FAXBOT_CONFIG_PATH`
- Atomic write: write to `*.tmp`, `fsync`, then `rename`
- Backups: keep `faxbot.config.json.bak`
- Validation: basic shape + per-plugin jsonschema when applying changes

### A.4 Create `plugins/registry.py`
Purpose: Discover Python plugins via entry points and validate manifests.

```bash
touch api/app/plugins/registry.py
```
Implementation notes:
- Use `importlib.metadata.entry_points(group="faxbot.plugins")`
- For each entry, load class, instantiate, call `manifest()`, validate against `manifest.schema.json`
- Return a list of `{ id, manifest, instance }` (instance kept internally)

### A.5 Create `routes/plugins.py`
Purpose: Expose Admin UI endpoints under `FEATURE_V3_PLUGINS=true`.

```bash
touch api/app/routes/plugins.py
```
Endpoints to implement:
- `GET /plugins`
- `GET /plugins/{id}/config`
- `PUT /plugins/{id}/config`
- `GET /plugin-registry` (reads `docs/plugin-registry.json`)

Mounting:
- Ensure router is included in main FastAPI app when flag enabled.

## B) Admin UI — Plugins Tab

Targets under `api/admin_ui/`:
- `src/api/plugins.ts`
- `src/pages/Plugins.tsx`
- Add route in `src/App.tsx`
- Add sidebar link in `src/components/Sidebar.tsx`

### B.1 Install dependencies
```bash
cd api/admin_ui
npm i @rjsf/mui @rjsf/validator-ajv8 @mui/material @emotion/react @emotion/styled
```

### B.2 Implement `src/api/plugins.ts`
Functions:
- `fetchPlugins()` → GET `/plugins`
- `getPluginConfig(id)` → GET `/plugins/{id}/config`
- `putPluginConfig(id, body)` → PUT `/plugins/{id}/config`
- `fetchPluginRegistry()` → GET `/plugin-registry`

### B.3 Implement `src/pages/Plugins.tsx`
UI behaviors:
- Show plugin cards with enable switch, schema-derived form, Save button, success/error toasts.
- Read-only form when disabled.
- Provide helper text + “Learn more” links per provider.

### B.4 Wire route and sidebar
- App route: `/plugins`
- Sidebar item: “Plugins”

## C) MCP — Dynamic Tools from Capabilities

Node MCP (`node_mcp/`):
- Add config loader to read `config/faxbot.config.json`.
- For `outbound` when enabled, register `send_fax` and `get_fax_status` tools that invoke REST API.
- For `inbound` when enabled, register `list_inbound` and `get_inbound_pdf`.
- Validate input/output using shared `docs/mcp_tools.schema.json`.

Python MCP (`python_mcp/`):
- Mirror Node logic using Pydantic for schemas.

Contract tests:
- Ensure both MCPs expose the same tool set for the same config.

## D) Security — Install Controls (Optional Feature)

Installer prerequisites when `FEATURE_PLUGIN_INSTALL=true`:
- `docs/plugin-allowlist.json` exists; entries include `{ source, name, version, sha256, signature? }`.
- Pinned public key for signature verification.

Install flow (server-side):
1) Validate requestor permissions (admin key).
2) Check allowlist for exact match.
3) Download package to temp dir; verify sha256; verify signature when provided.
4) For Python: run `pip install` in a constrained, non-interactive mode.
5) Reload discovery and return new manifest list.

Uninstall flow:
- For Python: `pip uninstall -y <package>`; update cache; reload discovery.

Safety:
- Dry-run preview with full log, require explicit confirmation flag from UI.
- Do not proceed on any verification failure.

## E) Migration Tooling

Create script: `scripts/migrate-config-v3.py`
Responsibilities:
- Read existing env vars and legacy config sources
- Produce `config/faxbot.config.json` with resolved providers
- Validate using per-plugin schema
- Write backups and print diff summary for operators

Run example:
```bash
python3 scripts/migrate-config-v3.py --out config/faxbot.config.json
```

## F) CI/CD Enhancements

Add workflow `.github/workflows/openapi-and-sdks.yml`:
- Lint `api/openapi.yaml` (e.g., Spectral)
- Verify server routes coverage vs spec (basic reachability)
- Generate SDKs on changes; commit or attach artifacts on PR

Add workflow `.github/workflows/mcp-parity.yml`:
- Boot Node + Python MCP against sample configs and assert identical tool sets

Add workflow `.github/workflows/plugins-security.yml`:
- Validate `docs/plugin-allowlist.json` entries (format, sha256 length)
- Block if `FEATURE_PLUGIN_INSTALL` code paths lack tests

## G) Testing Playbook

Unit tests (pytest):
- Config store atomic write + backup + revert
- Manifest schema validation errors produce 400 in `PUT /plugins/{id}/config`

Integration tests:
- Stub plugin implementing outbound; assert send/status path executed
- Registry endpoint returns curated JSON and filters are applied in UI

E2E tests (Playwright or Cypress):
- Enable/disable plugin toggles persist across reloads
- Validation errors surface inline and prevent save
- Docs “Learn more” links point to correct backend pages

Coverage targets:
- Core plugin code ≥ 85%
- Admin UI Plugins page ≥ 80% line coverage

---

# Part IV — OpenAPI Specification Details (Authoritative Contracts)

Purpose: Make `api/openapi.yaml` the single source of truth for all REST endpoints used by SDKs, MCPs, and Admin UI.

## IV.1 OpenAPI File Location and Structure

- Path: `api/openapi.yaml`
- Required top-level sections: `openapi`, `info`, `servers`, `tags`, `paths`, `components`

Bootstrap skeleton (replace placeholders):
```yaml
openapi: 3.0.3
info:
  title: Faxbot API
  version: 3.0.0
  description: REST API for fax transmission with plugin-based backends.
servers:
  - url: http://localhost:8080
tags:
  - name: fax
  - name: plugins
  - name: inbound
paths:
  /fax:
    post:
      tags: [fax]
      summary: Send a fax
      requestBody:
        required: true
        content:
          multipart/form-data:
            schema:
              type: object
              properties:
                to:
                  type: string
                file:
                  type: string
                  format: binary
              required: [to, file]
      responses:
        '200':
          description: Accepted
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/SendResult'
        '4XX':
          $ref: '#/components/responses/ErrorResponse'
  /fax/{id}:
    get:
      tags: [fax]
      summary: Get fax status
      parameters:
        - in: path
          name: id
          required: true
          schema:
            type: string
      responses:
        '200':
          description: OK
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/StatusResult'
        '404':
          $ref: '#/components/responses/ErrorResponse'
  /plugins:
    get:
      tags: [plugins]
      summary: List installed plugins and states
      responses:
        '200':
          description: OK
          content:
            application/json:
              schema:
                type: array
                items:
                  $ref: '#/components/schemas/PluginInfo'
  /plugins/{id}/config:
    get:
      tags: [plugins]
      summary: Get plugin config
      parameters:
        - in: path
          name: id
          required: true
          schema: { type: string }
      responses:
        '200':
          description: OK
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/PluginConfig'
    put:
      tags: [plugins]
      summary: Update plugin config
      parameters:
        - in: path
          name: id
          required: true
          schema: { type: string }
      requestBody:
        required: true
        content:
          application/json:
            schema:
              $ref: '#/components/schemas/PluginConfig'
      responses:
        '200': { description: Saved }
        '400':
          $ref: '#/components/responses/ErrorResponse'
  /plugin-registry:
    get:
      tags: [plugins]
      summary: Get curated plugin registry
      responses:
        '200':
          description: OK
          content:
            application/json:
              schema:
                type: array
                items:
                  $ref: '#/components/schemas/RegistryEntry'
components:
  schemas:
    SendResult:
      type: object
      properties:
        job_id: { type: string }
        backend: { type: string }
        provider_sid: { type: string, nullable: true }
      required: [job_id, backend]
    StatusResult:
      type: object
      properties:
        job_id: { type: string }
        status: { type: string, enum: [queued, in_progress, SUCCESS, FAILED] }
        pages: { type: integer, nullable: true }
        error: { type: string, nullable: true }
      required: [job_id, status]
    PluginInfo:
      type: object
      properties:
        id: { type: string }
        manifest: { $ref: '#/components/schemas/PluginManifest' }
        enabled: { type: boolean }
      required: [id, manifest, enabled]
    PluginManifest:
      type: object
      properties:
        id: { type: string }
        name: { type: string }
        version: { type: string }
        platform: { type: string, enum: [python, node] }
        categories: { type: array, items: { type: string } }
        capabilities: { type: array, items: { type: string } }
        config_schema: { type: object }
        defaults: { type: object }
      required: [id, name, version, platform, categories, capabilities, config_schema, defaults]
    PluginConfig:
      type: object
      properties:
        enabled: { type: boolean }
        settings: { type: object }
      required: [enabled, settings]
    RegistryEntry:
      type: object
      properties:
        id: { type: string }
        source: { type: string, enum: [npm, pypi] }
        package_name: { type: string }
        version: { type: string }
        keywords: { type: array, items: { type: string } }
        capabilities: { type: array, items: { type: string } }
        sha256: { type: string }
      required: [id, source, package_name, version]
  responses:
    ErrorResponse:
      description: Error
      content:
        application/json:
          schema:
            type: object
            properties:
              error: { type: string }
              details: { type: object }
```

Checklist:
- [ ] Ensure every live endpoint is defined and tagged.
- [ ] Add auth headers (`X-API-Key`) as a `securitySchemes` item and reference per path.
- [ ] Keep examples minimal and accurate; do not include PHI.

## IV.2 Server Validation Against Spec

- Add tests that call endpoints and validate response bodies against OpenAPI schemas using `openapi-core` or `schemathesis`.

---

# Part V — Admin UI Implementation Details

## V.1 Files and Folders

- `faxbot/api/admin_ui/src/pages/Plugins.tsx`
- `faxbot/api/admin_ui/src/api/plugins.ts`
- `faxbot/api/admin_ui/src/components/PluginCard.tsx` (optional extraction)

## V.2 State and Data Flow

- Load `GET /plugins` on page mount → populate list
- When user toggles `enabled`, immediately `PUT /plugins/{id}/config` with same `settings` and updated `enabled`
- When user edits fields and clicks Save, `PUT` with new `settings`
- Fetch registry: `GET /plugin-registry` on init; power search box

## V.3 UX Rules (Concrete)

- Show platform badge: python/node
- Read-only form when disabled
- Mask secrets using `ui:widget` password where schema marks `format: password`
- Helper text: Concise hints and a `Learn more` link to specific docs page for selected provider
- Error toasts include a “Fix it” link to troubleshooting docs

## V.4 Accessibility and Mobile

- Ensure tab order and `aria-*` labels for form fields
- Validate layout at 360px, 768px, and 1024px widths

---

# Part VI — Sample Plugins (Step-by-Step)

## VI.1 Python Sample: `faxbot_plugin_phaxio`

Scaffold:
```bash
mkdir -p samples/faxbot_plugin_phaxio/faxbot_plugin_phaxio
cat > samples/faxbot_plugin_phaxio/pyproject.toml << 'PY'
[project]
name = "faxbot_plugin_phaxio"
version = "0.1.0"
description = "Phaxio outbound fax plugin for Faxbot"
requires-python = ">=3.10"

[project.entry-points."faxbot.plugins"]
faxbot_plugin_phaxio = "faxbot_plugin_phaxio:PhaxioPlugin"
PY

cat > samples/faxbot_plugin_phaxio/faxbot_plugin_phaxio/__init__.py << 'PY'
from .plugin import PhaxioPlugin
__all__ = ["PhaxioPlugin"]
PY

cat > samples/faxbot_plugin_phaxio/faxbot_plugin_phaxio/plugin.py << 'PY'
from typing import Dict, Any
from api.app.plugins.base import OutboundPlugin, SendResult, StatusResult, PluginDeps

class PhaxioPlugin(OutboundPlugin):
    def __init__(self):
        self._cfg = None
        self._deps = None

    def manifest(self) -> Dict[str, Any]:
        return {
            "id": "faxbot_plugin_phaxio",
            "name": "Phaxio Outbound",
            "version": "0.1.0",
            "platform": "python",
            "categories": ["outbound"],
            "capabilities": ["send", "get_status"],
            "config_schema": {"type": "object", "properties": {"api_key": {"type": "string"}, "api_secret": {"type": "string"}}, "required": ["api_key", "api_secret"]},
            "defaults": {}
        }

    def validate_config(self, config: Dict[str, Any]) -> None:
        if not config.get("api_key") or not config.get("api_secret"):
            raise ValueError("api_key and api_secret are required")

    def start(self, config: Dict[str, Any], deps: PluginDeps) -> None:
        self._cfg = config
        self._deps = deps

    def stop(self) -> None:
        self._cfg = None
        self._deps = None

    def send(self, to_number: str, file_path: str) -> SendResult:
        # Placeholder: call Phaxio API via core HTTP client in deps
        return SendResult(job_id="demo", backend="phaxio", provider_sid=None)

    def get_status(self, job_id: str) -> StatusResult:
        return StatusResult(job_id=job_id, status="queued", pages=None, error=None)
PY
```

Install locally for testing:
```bash
pip install -e samples/faxbot_plugin_phaxio
```

## VI.2 Node Sample: `@faxbot/plugin-helper-devtools`

Scaffold:
```bash
mkdir -p samples/node/@faxbot/plugin-helper-devtools
cat > samples/node/@faxbot/plugin-helper-devtools/package.json << 'JSON'
{
  "name": "@faxbot/plugin-helper-devtools",
  "version": "0.1.0",
  "keywords": ["faxbot-plugin"],
  "faxbot": {
    "manifest": {
      "id": "@faxbot/plugin-helper-devtools",
      "name": "Helper DevTools",
      "version": "0.1.0",
      "platform": "node",
      "categories": ["helper"],
      "capabilities": [],
      "config_schema": {"type": "object", "properties": {}},
      "defaults": {}
    }
  }
}
JSON
```

---

# Part VI.A — Storage Plugin Example (S3) — Full Schema, Tips, Links

Name
- Python: `faxbot_plugin_s3`
- Node helper (optional): `@faxbot/plugin-s3-helper` (no PHI; UI helpers only)

Manifest (Python)
```json
{
  "id": "faxbot_plugin_s3",
  "name": "S3 Storage",
  "version": "0.1.0",
  "platform": "python",
  "categories": ["storage"],
  "capabilities": ["put", "get"],
  "config_schema": {
    "type": "object",
    "properties": {
      "bucket": { "type": "string", "description": "S3 bucket name" },
      "region": { "type": "string" },
      "endpoint_url": { "type": "string", "format": "uri", "description": "S3-compatible endpoint (MinIO)" },
      "access_key_id": { "type": "string" },
      "secret_access_key": { "type": "string" },
      "kms_key_id": { "type": "string", "description": "Use SSE-KMS when provided" }
    },
    "required": ["bucket"],
    "additionalProperties": false
  },
  "defaults": { "region": "us-east-1" },
  "ui": {
    "hints": [
      "For PHI, use SSE-KMS and restrict bucket access with IAM policies.",
      "Use an S3-compatible endpoint like MinIO for on-prem deployments."
    ],
    "links": [
      { "title": "Faxbot Storage Docs", "url": "https://docs.faxbot.dev/backends/storage" },
      { "title": "AWS S3 SSE-KMS", "url": "https://docs.aws.amazon.com/AmazonS3/latest/userguide/UsingKMSEncryption.html" },
      { "title": "MinIO Server", "url": "https://min.io/docs/" }
    ],
    "uiSchema": {
      "secret_access_key": { "ui:widget": "password" }
    }
  }
}
```

Implementation notes
- Provide `put(path, data)` and `get(path)` methods; core will orchestrate naming and retention windows.
- Respect timeouts and retries; redact credentials in logs.

# Part VII — Security Hardening and Supply Chain

## VII.1 Allowlist Format: `docs/plugin-allowlist.json`

Example entry:
```json
[
  {
    "id": "faxbot_plugin_phaxio",
    "source": "pypi",
    "package_name": "faxbot_plugin_phaxio",
    "version": "0.1.0",
    "sha256": "<64-hex>",
    "signature": "<base64-der>"
  }
]
```

## VII.2 Verification Workflow

Steps:
1) Match id/source/name/version with allowlist
2) Download wheel/sdist to temp
3) Compute sha256 and compare
4) If signature present, verify with pinned public key
5) Only then install

## VII.3 Key Rotation

- Keep a `SECURITY_KEYS.md` doc describing active and retired keys
- Allow multiple public keys for a grace period; deny after cutoff date

---

# Part VIII — Admin UI Plugin Search & Install (Full Implementation Guide)

Goal: Provide a Scrypted-like local UI where operators can search a curated registry and optionally install plugins safely.

Feature flags
- `FEATURE_V3_PLUGINS=true` (enables Plugins tab and discovery)
- `FEATURE_PLUGIN_INSTALL=false` by default (gates install flows)

## VIII.1 Backend Additions (API)

Endpoints (add to Part IV OpenAPI):
- `POST /plugins/install` body `{ source: "npm"|"pypi", name: string, version?: string }` → `202 Accepted` with `{ install_id: string }`
- `GET /plugins/install/{install_id}` → `{ status: "queued"|"running"|"success"|"error", logs: string[] }`
- `POST /plugins/uninstall` body `{ source: "npm"|"pypi", name: string }` → `200` on success

Implementation
- Use FastAPI BackgroundTasks or a lightweight queue to run installs. Maintain an in-memory `installs: Dict[str, {status, logs[]}>` and write logs to `faxbot/faxdata/install-logs/<install_id>.log` for persistence.
- Enforce allowlist (Part VII) and verify `sha256` (and signature if provided) before executing any install command.
- Python installs only for backend plugins: `python3 -m pip install --disable-pip-version-check --no-input --no-cache-dir <package>==<version>`.
- Node installs optional for Node-side helper plugins (no PHI): `npm i --no-audit --no-fund --silent <package>@<version>`.
- After success, re-run plugin discovery and expose new manifests in `GET /plugins`.

## VIII.2 Admin UI Pages and Components

New files under `faxbot/api/admin_ui/`:
- `src/pages/PluginSearch.tsx` — search registry and show result cards
- `src/components/InstallModal.tsx` — dry-run preview, confirmation, progress logs
- Extend `src/api/plugins.ts` — `installPlugin`, `getInstallStatus`, `uninstallPlugin`

Data flow
- Load `GET /plugin-registry` on mount; filter client-side by keyword/capability.
- If `FEATURE_PLUGIN_INSTALL=false`, primary action shows “How to install” with copyable CLI commands.
- If `FEATURE_PLUGIN_INSTALL=true`, show “Install” button that opens `InstallModal`.

InstallModal
- Shows allowlist state and dry-run commands.
- Requires explicit checkbox: “I approve running these commands.”
- Calls `POST /plugins/install`; polls `GET /plugins/install/{install_id}` every 1s; streams logs.
- On `success`, show CTA “Enable in Plugins tab”.

Uninstall
- In `Plugins` tab kebab menu: “Uninstall” (only when disabled). Confirms, calls `POST /plugins/uninstall`.

UX copy
- Banner: “Installing third-party code expands your attack surface. Use verified plugins only. Learn more.” → `docs/PLUGIN_SECURITY.md`.

## VIII.3 API Client (UI)

Add to `src/api/plugins.ts`:
```ts
export async function installPlugin(body: { source: 'npm'|'pypi'; name: string; version?: string }) {
  const res = await fetch('/plugins/install', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
  if (!res.ok) throw new Error('install failed');
  return res.json() as Promise<{ install_id: string }>;
}

export async function getInstallStatus(installId: string) {
  const res = await fetch(`/plugins/install/${installId}`);
  if (!res.ok) throw new Error('status failed');
  return res.json() as Promise<{ status: 'queued'|'running'|'success'|'error'; logs: string[] }>;
}

export async function uninstallPlugin(body: { source: 'npm'|'pypi'; name: string }) {
  const res = await fetch('/plugins/uninstall', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
  if (!res.ok) throw new Error('uninstall failed');
}
```

## VIII.4 OpenAPI Additions

- Add `/plugins/install`, `/plugins/install/{install_id}`, `/plugins/uninstall` to `api/openapi.yaml` with request/response schemas per above.

## VIII.5 Plugin Detail Page (Scrypted-like)

UX
- Route: `/plugins/:id`
- Header with plugin name, version, platform badge, enable toggle, Uninstall (disabled when enabled)
- Sections:
  - Settings (JSON Schema form rendered from `manifest.config_schema`)
  - Tips (short bullets from `manifest.ui.hints`)
  - Learn more (links from `manifest.ui.links` with validated domains)
  - Troubleshooting (contextual links based on capability/backend)

Admin UI changes
- Create `src/pages/PluginDetail.tsx` and navigate from card “Configure” button
- Reuse `getPluginConfig`/`putPluginConfig`; show toasts on save; “Reset to defaults” using `manifest.defaults`

Backend
- `GET /plugins/{id}` returns `PluginInfo` including `manifest` and current config (optional convenience endpoint). Otherwise, client composes from `/plugins` and `/plugins/{id}/config`.

OpenAPI
- Add `GET /plugins/{id}` that returns `PluginInfo`.

---

# Part IX — Refactoring Existing Backends into Plugins (Cookbook)

---

# Part X — Plugin Publishing and Naming (npm/PyPI) and Parity Rules

Naming conventions
- Python (backend-executed plugins): `faxbot_plugin_<shortname>` (e.g., `faxbot_plugin_phaxio`, `faxbot_plugin_s3`)
- Node helpers (no PHI, MCP/UI tooling): `@faxbot/plugin-<shortname>` (preferred scope) or `faxbot-plugin-<shortname>`

Versioning
- SemVer required. Breaking changes to `config_schema` or capabilities require a major bump.

Publishing checklist (PyPI)
- `pyproject.toml` with `project.entry-points."faxbot.plugins"` mapping id → module:Class
- `README.md` with capability list and config table
- `CHANGELOG.md` with security notes
- `LICENSE` (Apache-2.0 recommended)

Publishing checklist (npm)
- `package.json` with `keywords: ["faxbot-plugin"]` and `faxbot.manifest`
- No PHI-handling code; helpers only

Parity rules
- Capabilities and tool names must match those exposed in MCP and OpenAPI.
- UI metadata (`ui.hints`, `ui.links`, `uiSchema`) should be provided for both Python and Node plugins when applicable so Admin UI renders consistent experiences.


Goal: Extract provider-specific logic into Python plugins while keeping REST contracts stable.

Current source (reference):
- Phaxio: `api/app/phaxio_service.py`
- Sinch: `api/app/sinch_service.py`
- SIP/Asterisk: `api/app/ami.py` (and `conversion.py`)
- Glue: `api/app/main.py`, `config.py`, `models.py`, `storage.py`

## IX.1 Strategy

1) Create plugin packages under `samples/` initially; later publish to PyPI.
2) Move provider functions into `OutboundPlugin` methods `send`/`get_status`.
3) `manifest().config_schema` mirrors previous env vars; secrets marked as password in UI via `uiSchema`.
4) Add `api/services/provider_adapter.py` that returns active plugin instances from discovery + resolved config.
5) Replace direct service calls in `main.py` with adapter calls.
6) Migrate tests to use plugin or stub via adapter.

## IX.2 Phaxio → `faxbot_plugin_phaxio`

From: `phaxio_service.py` (HTTP to Phaxio, tokenized PDF URL)
To plugin methods: `send`, `get_status`.
Config schema fields: `api_key`, `api_secret`, `callback_url`, `verify_signature`.
Use `PluginDeps.storage` to get a tokenized PDF URL; keep token creation in core.

Steps:
- Copy HTTP client logic; replace env reads with `self._cfg[...]`.
- Add unit tests for error mapping and status transitions.
- Core `main.py`: delegate via `get_outbound_plugin().send(...)`.

## IX.3 Sinch → `faxbot_plugin_sinch`

From: `sinch_service.py`.
Schema: `project_id`, `api_key`, `api_secret`, `base_url?`.
Mirror Phaxio extraction; preserve error codes and headers.

## IX.4 SIP/Asterisk → `faxbot_plugin_sip`

From: `ami.py` (+ `conversion.py`).
Schema: `ami_host`, `ami_port`, `ami_username`, `ami_password`, `sip_username`, `sip_password`, `sip_server`, `from_number`.
Expose `convert_pdf_to_tiff(path)` via `PluginDeps` from core to avoid duplication.

## IX.5 Test Mode → `faxbot_plugin_test`

Always return `SUCCESS` after a short delay; no external dependencies.

## IX.6 Core Edits

Add `api/services/provider_adapter.py`:
- `get_outbound_plugin() -> OutboundPlugin`
- `get_inbound_plugin() -> InboundPlugin | None`
- Cache instances by slot; reload on config change

Edit `api/app/main.py`:
- Replace uses of `phaxio_service`/`sinch_service`/`ami` with adapter calls.

Edit `api/app/config.py`:
- When `FEATURE_V3_PLUGINS=true`, load `services/config_store.get_resolved_config()`; otherwise legacy env.

Tests:
- Update `api/tests/test_phaxio.py` to validate through plugin or adapter stubs.

## IX.7 Migration Commands

```bash
# Adapter
touch api/app/services/provider_adapter.py

# Sample plugin skeletons
mkdir -p samples/faxbot_plugin_sinch/faxbot_plugin_sinch
mkdir -p samples/faxbot_plugin_sip/faxbot_plugin_sip
mkdir -p samples/faxbot_plugin_test/faxbot_plugin_test

# Editable installs for local dev
pip install -e samples/faxbot_plugin_phaxio
pip install -e samples/faxbot_plugin_sinch
pip install -e samples/faxbot_plugin_sip
pip install -e samples/faxbot_plugin_test
```

## IX.8 Rollback

- Keep legacy modules active behind the feature flag during Phase 1.
- If plugin load fails, fall back to legacy paths and surface an Admin UI warning with remediation links.
