# Phase 4 Runbook — Enterprise Integration & Marketplace (auto-tunnel)

Branch policy

- Work only in `auto-tunnel`; open PRs with base `auto-tunnel`.
- CI must be green (OpenAPI gen, traits schema, greps, unit tests) before merge.
- Auto‑merge is enabled in GitHub. Use `gh pr merge --auto` or let the repo rule merge after checks.

PR loop (repeat per task)

1) Branch off `auto-tunnel`:
   - `git checkout auto-tunnel && git pull`
   - `git checkout -b feat/p4-<short>`
2) Make a small, atomic change (files listed in the task).
3) Commit and push:
   - `git add -A && git commit -m "p4(<scope>): <summary>"`
   - `git push -u origin HEAD`
4) Open PR targeting `auto-tunnel`:
   - `gh pr create -B auto-tunnel -t "p4(<scope>): <title>" -b "Implements Phase 4: <details>"`
5) Wait for CI to complete and merge when green:
   - `gh pr checks --watch --interval 120`
   - `gh pr merge --auto --squash` (or allow repo auto‑merge)
6) Sync base branch:
   - `git checkout auto-tunnel && git pull`

Tasks (Phase 4 initial milestones)

1) Admin Console — Plugin Marketplace (read‑only skeleton)
   - Add `api/admin_ui/src/components/PluginMarketplace.tsx` (compiles with strict TS; not yet wired in UI).
   - No provider name checks; trait‑gated display in later PR.
   - CI: UI builds (`npm ci && npm run build`).

2) Admin API — Marketplace listing endpoint (disabled by default)
   - Add router `api/app/routers/admin_marketplace.py`:
     - `GET /admin/marketplace/plugins` returns `{ "plugins": [] }` with admin auth dependency.
     - Gated by `ADMIN_MARKETPLACE_ENABLED=false` (returns 404 when disabled).
   - Wire router in `app.main` under a guarded import.
   - CI: OpenAPI regenerates; greps remain green; tests unaffected.

3) Identity integrations (LDAP/SAML) — skeleton providers (no imports at startup)
   - Add files under `api/app/plugins/identity/providers/{ldap,saml}/` implementing classes that DO NOT subclass `IdentityPlugin` directly (avoid CI grep failure).
   - Keep third‑party imports inside functions to prevent import‑time errors.
   - No wiring or manifests yet.

4) Webhook delivery enhancements — scaffolding only
   - Add `api/app/events/webhooks_enterprise.py` (not imported by default).
   - Include dataclasses and TODOs; no external effects.

Acceptance and guardrails (per PR)

- Contracts job passes:
  - Provider traits schema validation.
  - OpenAPI generation succeeds; pinned diff skipped if no snapshot.
- Greps (`scripts/ci/greps.sh`) pass:
  - One `ProviderHealthMonitor` class.
  - No provider name checks in Admin UI.
  - 202 Accepted present for inbound callbacks.
  - CSRF middleware present and referenced.
  - Exactly one `IdentityPlugin` implementation.
- Unit tests (`.github/workflows/ci.yml`) pass.

Notes

- Admin Console stays API‑key login; do not enable sessions in Phase 4 tasks.
- Keep all new endpoints additive; do not rename or remove legacy routes.
- Never log PHI; log job IDs only.
