# Phase 2 – Trait-Based Auth & User Management: Execution Checklist (auto-tunnel)

This checklist is the compact, actionable companion to:
- v4_plans/implement/phase_2_implement_plan.md (canonical spec)
- v4_plans/implement/implementation-runbook.md (PR loop + smoke patterns)

Branch policy
- Work only in `auto-tunnel`. Open PRs targeting `auto-tunnel`.
- Keep existing routes stable (no renames), maintain API-key flows while adding sessions.
- Webhooks continue to ACK fast (202 prod / 200 test via helper).

Environment and flags (dev defaults; enforce in CI as needed)
- FAXBOT_SESSIONS_ENABLED=false
- FAXBOT_CSRF_ENABLED=false
- CONFIG_MASTER_KEY=<44-char base64> (required; fail-fast mode)
- FAXBOT_SESSION_PEPPER=<random hex 64> (required; fail-fast mode)
- FAXBOT_BOOTSTRAP_PASSWORD=<one-time admin password> (optional; dev bootstrap only)

Status key
- [ ] pending  [~] in progress  [x] done

P2-01 Async SQLAlchemy invariants (P0)
- [x] Add async DB layer: `api/app/db/async.py` (async engine + `AsyncSessionLocal`)
- [ ] Ensure identity/session code uses `AsyncSession` + `select()`; no blocking `.query()`
- [x] Dev-only DDL helper guarded (prefer Alembic)
Acceptance
- `rg -n "db\.query\(" api/app | wc -l` → 0 in new identity/session code
- Example helper present in code comments or tests (select/await pattern)

P2-02 Identity plugin base + provider skeleton (P0)
- [x] `api/app/plugins/identity/base.py`: `IdentityPlugin(FaxbotPlugin)`, dataclasses (User, Group, Session, AuthResult)
- [x] Enforce `plugin_type = "identity"` at load-time; manager supports `get_active_by_type("identity")`
- [x] Minimal provider stub wired (e.g., `identity/sqlalchemy.py`)
Acceptance
- `rg -n "class IdentityProvider|class IdentityPlugin" api/app | wc -l` → 1–2 total (no duplicates)
- `python -c "from api.app.plugins.manager import PluginManager; pm=PluginManager(); pm.load_all(); print(pm.get_active_by_type('identity'))"` succeeds

P2-03 Alembic migration for users/groups/sessions (P0)
- [ ] Create users, groups, user_groups, user_sessions tables
- [ ] Autogenerate or explicit migration; document dev fallback DDL
Acceptance
- Migration file exists under `migrations/versions/*phase2*`
- `alembic upgrade head` applies cleanly in CI/dev

P2-04 Session storage (peppered hash) + helpers (P0)
- [x] Store only peppered hash of session token; never persist raw
- [x] Rotation capability (on login/elevation)
Acceptance
- `rg -n "_hash_token|FAXBOT_SESSION_PEPPER" api/app | wc -l` → >0
- Unit/integration smoke proves validate/revoke via hash

P2-05 CSRF middleware for cookie sessions (P0)
- [x] `api/app/middleware/csrf.py` added; mounted only when `FAXBOT_CSRF_ENABLED=true`
Acceptance
- `rg -n "class CSRFMiddleware" api/app` → 1
- POST without header when cookie present → 403 (dev toggle), unchanged for API-key flows

P2-06 Auth endpoints (dual-mode; API-key preserved)
- [x] `/auth/login` (sets HttpOnly, Secure, SameSite=Strict cookie; returns CSRF token if enabled)
- [x] `/auth/logout` (cookie clear + revoke)
- [x] `/auth/refresh` (rotate + extend expiry)
Acceptance
- API keys continue to work unchanged
- Cookies present only when sessions enabled; CSRF enforced only when flag true

P2-07 Permission grammar + trait mapping
- [x] Canonical permission format `{namespace}.{resource}:{action}`
- [x] Legacy scopes → canonical permissions mapper; canonical → legacy scopes mapper
- [x] Demo guarded route using permissions (non-breaking)
Acceptance
- `rg -n "permissions_to_legacy_scopes" api/app` → 1
- One guarded route uses permission check (non-breaking demo)

P2-08 Group hierarchy safety
- [ ] Cycle detection + max depth
- [ ] Precompute flattened traits for user via groups
Acceptance
- Unit test or small helper validates cycle detection path

P2-09 UI config endpoint + caching
- [x] `/admin/ui-config` returns minimal config for Admin Console with ETag
Acceptance
- First GET returns ETag; subsequent GET with `If-None-Match` → 304

P2-10 Bootstrap admin (no lockout)
- [x] Bootstrap via `FAXBOT_BOOTSTRAP_PASSWORD` (dev/stage only); create `admin` if missing
Acceptance
- First-run admin exists when env set; re-run does not duplicate

P2-11 Admin Console session UI (feature-gated)
- [ ] Add login form and session indicators; disabled by default (API-key remains primary)
- [ ] CSRF header wiring when sessions are enabled
Acceptance
- Feature flag off by default; no UI breakage

P2-12 Fail-fast secrets at startup
- [x] Fail with actionable error if `CONFIG_MASTER_KEY` or `FAXBOT_SESSION_PEPPER` missing (when sessions enabled)
Acceptance
- `rg -n "CONFIG_MASTER_KEY|FAXBOT_SESSION_PEPPER" api/app` → checks present

CI guardrails (extend Phase 1 checks)
- [ ] Add greps for: `db.query\(` banned in `identity/*` when async mode active
- [ ] Ensure exactly one `IdentityPlugin` provider class
- [ ] Validate CSRF middleware presence when sessions enabled in tests

Suggested PR sequencing (into auto-tunnel)
- P2-01: Async DB layer + identity bases
- P2-02: Alembic + peppered sessions
- P2-03: CSRF middleware + auth endpoints (dual-mode)
- P2-04: Permissions/traits mapping + one guarded route
- P2-05: UI config endpoint + Admin Console session shell (flagged)
- P2-06: Bootstrap admin + cycle safety + docs

Smoke snippets (dev)
```bash
# Async DB import (no block)
python - <<'PY'
from sqlalchemy import select
from api.app.db.async import AsyncSessionLocal
async def main():
    async with AsyncSessionLocal() as db:
        await db.execute(select(1))
print('ok')
PY

# CSRF toggle check (when sessions enabled)
curl -i -X POST http://localhost:8080/admin/providers/test || true
```

Notes
- Keep Admin routes stable; add parallel /auth/* only.
- Never log PHI or secrets; audit events use job/user IDs only.
- Admin Console remains API-key primary until Phase 2 close-out.
