# Faxbot v3 Refactor Plan — Plugins, MCP, SDKs, and Admin UI

Audience: This document is written for agents and developers, including low-parameter LLMs with a knowledge cutoff before MCP existed. It contains explicit, step-by-step instructions with no assumed knowledge. Follow the steps in order. Copy code blocks exactly. Do not improvise names, paths, or JSON keys.

Status: Draft on development branch. All work must be done on feature branches that branch off of development, then merged back into development via PRs. Do not push directly to main. A formal migration and release plan appears near the end.

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
- main: stable releases only. No direct pushes. Only fast-forward merges from release branches.
- development: integration branch for v3. PRs merge here after CI passes.
- feature/*: per-task branches cut from development. Example: feature/plugins-backend-api. Merge into development via PR.
- release/v3.x: created when v3 is feature-complete on development. Used to stage releases and backports.

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
- send → tool name: fax.send
- get_status → fax.get_status
- list_inbound → fax.inbound.list
- get_inbound_pdf → fax.inbound.get_pdf
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
- Example: if outbound.send exists, register fax.send and wire to plugin adapter.

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