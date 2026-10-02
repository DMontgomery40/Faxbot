# Faxbot Provider Configuration Paths Implementation Plan

> **For agentic workers:** Use superpowers:subagent-driven-development for this bounded implementation and independent review. Primary owns subsequent configuration activation architecture.

**Goal:** Provider traits, installed manifests, plugin discovery/configuration, dispatch and status refresh use the same intended paths from source or the actual packaged image, independent of the process working directory.

**Architecture:** One small paths module owns bundled resource locations and explicit path overrides. Existing config and main consumers call it instead of constructing separate cwd paths. Resolve a provider identifier as one safe path component beneath the configured provider root; preserve existing API response shapes and valid explicit configuration. This is an independently testable repair within the approved configuration work, not completion of safe provider selection/activation/rotation.

**Tech Stack:** Existing Python3.11/FastAPI/Pydantic stack, pathlib, pytest; no new dependencies.

## Context and constraints

- Entire refresh design is user approved. No further design permission or approval question.
- Integration worktree /Users/davidmontgomery/.codex/worktrees/faxbot-refresh/Faxbot, branch feat/faxbot-refresh. Start productBASEdcd4c442 (resolve full SHA).
- Product writer: bounded Sol6.1xhigh implementer for this task only. Primary does read-only activation architecture; no concurrent product edits.
- Source app can import as api.app or app; Docker copies API code to /app/app and config assets to /app/config. Both must resolve correctly. Do not guess based on cwd or on import package spelling.
- Existing explicit overrides FAXBOT_PROVIDERS_DIR,FAXBOT_CONFIG_PATH,PLUGIN_REGISTRY_PATH remain supported. Document relative override semantics. Keep an explicit operator path meaningful; default resources are package-relative.
- Scope paths only. Do not rewrite Settings activation, cached services, generic manifest execution, secret persistence, providers/adapters, database, workers or auth/RBAC here. Do not label this a complete configuration module.
- Preserve API fields/response shapes and current valid directories. No production credentials, provider network calls, real fax send, publication, deploy or outreach. Synthetic tests only.

## File responsibilities

- New api/app/config_paths.py: bundled application/config location; resolved providers/config/registry locations; validated provider manifest path. No config.py dependency/cycle.
- api/app/config.py: traits/manifest loader paths and default config path use helper. Retain unrelated behavior until its explicit replacement.
- api/app/main.py: installation/import, inventory/diagnostics, plugin config/discovery, send preparation, manifest dispatch and job refresh use helper. Same existing override everywhere. Scraped bundled plugin examples resolve their actual module resource location. Do not change UI asset routing/terminal paths.
- New api/tests/test_config_paths.py: real public/helper behavior from different cwd and actual install/discovery/read consistency. Cover explicit custom provider directory and invalid path IDs. Current related API tests must remain meaningful and pass.
- Evidence and source comments document deployment-relative and override semantics; product generated docs remain Docs Autopilot owned.

### Task 1: Consistent provider resource locations

- [x] Record exact BASE, inspect all related consumers and existing overrides; no broad reinventory.
- [x] RED: source cwd root/api/unrelated directory yields identical built-in traits/kinds and default settings path. Use real resource loading, not monkeypatched registry responses.
- [x] RED: installing a synthetic manifest into FAXBOT_PROVIDERS_DIR makes it visible to traits, inventory/config, dispatch manifest lookup and historical status refresh. Capture actual selected file identity; no provider network call.
- [x] RED: invalid/absolute/traversal/encoded path-component IDs cannot write/read outside provider root; preexisting escaped symlink paths are refused. Preserve legitimate IDs and an explicitly configured root (which may itself resolve through an operator symlink). Use fixed safe errors; install invalid input returns400, bulk import reports invalid item without external file mutation.
- [x] Implement single module and replace every provider/traits/config/registry cwd lookup. Do not hide path failures in new silent fallback logic.
- [x] Test source import styles and flattened runtime layout; preserve explicit overrides including documented relative behavior.
- [x] Run focused tests then full API suite in approved runtime environment. Diagnose new real behavior exposed by loading correct traits; do not weaken tests or restore broken cwd dependence just to pass.
- [x] Report exact code/source/tests and remaining unrelated selection/rotation findings. Commit only owned product/test files with @codex review in body, leave task report in scratch, obtain independent fixed-range review. Primary verifies actual image path behavior before closing task.

## Remaining configuration work after this task

A single validated effective provider/account identity; atomic typed configuration snapshots; real runtime activation or explicit staged/restart behavior; safe persisted settings and secret handling; invalid selection refusal; credential cache invalidation and in-flight/historical provider binding; truthful capability/diagnostic/UI responses. Preserve built-in Documo and installed manifest workflows. Durable delivery and RBAC remain required under the active whole-product goal.

Completion: independently approved source `48c9ff96`; full258passed/13dialect skips, zero warnings; actual image proof and carried activation defects are recorded in `../../modernization/evidence/2026-10-02-provider-paths.md`.
