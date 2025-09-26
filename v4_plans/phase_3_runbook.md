# Faxbot v4 — Phase 3 Runbook (Auto‑Tunnel)

Scope: Hierarchical configuration, diagnostics, and enterprise reliability. This runbook drives PR16–PR21 using the v4 PR loop, with Admin Console coverage and strict adherence to AGENTS.md (traits‑first, plugin‑first, HIPAA).

Branch policy
- Base branch: `auto-tunnel`
- Per‑feature branches: `feat/prXX-<short>` → PR to `auto-tunnel`
- Require green CI; never commit to `main`. After milestones, merge `auto-tunnel` → `development` via PR only.
- PR loop: see `v4_plans/implement/pr-loop-per-phase.md:1` (use exactly that 8‑step loop).

Pre‑checks (one‑time per workstation)
- Secrets: ensure `CONFIG_MASTER_KEY` (44‑char base64) and `FAXBOT_SESSION_PEPPER` are set for secure modes; app should fail fast if sessions are enabled and these are missing.
- Optional: `REDIS_URL=redis://redis:6379/0` for caching and rate limiting.
- Dev run: `docker compose build && docker compose up -d` then `curl -fsS http://localhost:8080/health || true`.
- Quick greps: `bash scripts/ci/greps.sh || true`.

Current repository state (verified locally)
- Admin diagnostics: `/admin/diagnostics/run` present and working.
- Admin UI config: `/admin/ui-config` present.
- v3 Admin effective config: `/admin/config/effective` present.
- NOT FOUND in workspace (expected from PR14/PR15 notes):
  - `api/app/config/hierarchical_provider.py`
  - `api/app/services/cache_manager.py`
  - v4 Admin config endpoints under `/admin/config/v4/*` (effective, hierarchy, safe-keys, flush-cache)
  - Admin SSE diagnostics router (`api/app/routers/admin_diagnostics.py`) and `services/events.py`

Implication: Before PR16 (UI for hierarchical config) we must land PR14/PR15 content or re‑implement them here. If you have those changes in an open PR elsewhere, cherry‑pick or recreate them as small, testable diffs.

Deliverables by PR

PR15 — Admin config v4 endpoints (read‑only) + hierarchical provider bootstrap
- Backend
  - Add `api/app/config/hierarchical_provider.py` (async provider with DB‑first resolution, `.env` fallback on DB outage only; Fernet encryption with `CONFIG_MASTER_KEY`; masked outputs; precise cache keys).
  - Add `api/app/services/cache_manager.py` (Redis + local fallback; TTL; targeted invalidation by scope and key).
  - Wire lazy bootstrap in `api/app/main.py` (search anchor: “Phase 3: optional hierarchical config bootstrap (lazy)”).
  - Add v4 endpoints (read‑only) alongside legacy (do not rename existing):
    - `GET /admin/config/v4/effective`
    - `GET /admin/config/v4/hierarchy`
    - `GET /admin/config/v4/safe-keys`
    - `POST /admin/config/v4/flush-cache?scope=...`
  - Emit no PHI; secrets masked; return 202 where applicable (not required for these reads).
- Dependencies
  - Confirm `api/requirements.txt` has `cryptography`, `redis`, `sse-starlette`, and pinned `anyio` (bumped to 4.11.0). Run tests to ensure no asyncio regressions.
- Acceptance
  - Greps pass; OpenAPI diff stable; endpoints functional with DB present and with DB deliberately unavailable (env fallback used only in outage).

PR16 — Admin Console: ConfigurationManager (read‑only)
- UI
  - Create `api/admin_ui/src/components/ConfigurationManager.tsx`.
  - Fetch from `GET /admin/config/v4/effective`, `/hierarchy`, `/safe-keys`; call `/flush-cache` on demand.
  - Display source badges (db/env/default/cache); hierarchy stack; secrets masked; helper text + “Learn more” links from `docsBase`.
  - Trait‑gate visibility using existing user traits hooks; no provider name checks.
  - Wire into App shell (new Settings sub‑tab “Configuration” or under Tools if specified by plan) and add client methods in `api/admin_ui/src/api/client.ts`.
- Acceptance
  - Admin UI builds in Dockerfile stage; component renders real data; mobile responsive; no traits violations.

PR17 — Safe write path + cache invalidation
- Backend
  - Add `POST /admin/config/v4/set` to update only SAFE_EDIT_KEYS.
  - Enforce masking + `config_audit` with masked diffs and `value_hmac` (HMAC‑SHA256 with server‑side pepper).
  - Invalidate cache precisely by scope and key.
- UI
  - “Save” actions enabled only for safe keys; show success and guidance; invoke flush‑cache as needed.
- Acceptance
  - Values persist in DB; effective view updates without logging secrets.

PR19 — Provider health monitor + circuit breaker
- Backend
  - Implement exactly one `ProviderHealthMonitor` class in `api/app/monitoring/health.py` using `plugin_manager` and hierarchical thresholds/timeouts (`get_effective(...)`).
  - Emit canonical events on transitions; surface snapshots in diagnostics.
- Acceptance
  - Grep requires one provider health monitor; transitions observed under simulated failures.

PR20 — Webhook hardening + DLQ + idempotent 202
- Backend
  - Verify signatures via trait (`webhook.verification`); dedupe on `provider_id + external_id`.
  - Persist DLQ on validation failures (headers allowlist only — no secrets).
  - All inbound handlers return 202 and are idempotent.
- Acceptance
  - CI grep confirms 202; DLQ populated on invalid payloads; duplicates deduped.

PR21 — Rate limiting (hierarchical)
- Backend
  - Middleware enforces per‑user/per‑endpoint RPM; Redis preferred; headers: `Retry-After`, `X-RateLimit-*`.
  - RPM value from `api.rate_limit_rpm` via hierarchical config.
- Acceptance
  - 429s with headers observed under load; config overrides respected.

Coding guardrails (Phase 3)
- Traits over names; `plugin_manager.get_active_by_type()` and trait checks only.
- DB‑first config; `.env` fallback only on DB outage.
- Async everything; avoid blocking I/O in async paths.
- Admin Console coverage required; mobile‑first; inline help + docs links.
- No PHI or secrets in logs/events/SSE; audit uses masked values + HMAC.
- Do not rename legacy routes; add new routes in parallel only.
- Exactly one `/metrics` endpoint; exactly one `ProviderHealthMonitor` class.

Local validation steps (repeat per PR)
1) Greps and quick health
   - `bash scripts/ci/greps.sh || true`
   - `docker compose build && docker compose up -d`
   - `curl -fsS http://localhost:8080/health || true`
2) UI build (Dockerfile first stage builds Admin UI)
3) OpenAPI diff and tests (CI)

Rollback (if a PR breaks CI)
1) `git checkout auto-tunnel && git pull`
2) `git log --oneline` and copy merge commit SHA
3) `git revert -m 1 <merge-commit-sha>` then `git push`

Next actions from this runbook
1) If missing, implement PR15 (hierarchical provider + cache + v4 read‑only endpoints) as tight diffs.
2) Implement PR16: add `ConfigurationManager.tsx` and AdminAPIClient methods; wire into App.
3) Proceed with PR17–PR21 as above.

