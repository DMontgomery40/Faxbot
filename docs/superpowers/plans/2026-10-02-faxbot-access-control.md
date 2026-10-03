# Faxbot Access Control Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Complete named user/integration RBAC, persistent sessions and scoped fax/mailbox access across the retained Faxbot product.

**Architecture:** One access module owns credential verification, current policy, resource visibility and authorized mutations. Versioned relational state and atomic security audit support multiple workers; adapters translate its decisions without recreating them. Preserve captured outbound delivery and existing key replay identities.

**Tech Stack:** Python3.11, FastAPI, SQLAlchemy/Alembic, SQLite/PostgreSQL, existing scrypt hashing; React/TypeScript/MUI console.

**Spec:** `docs/superpowers/specs/2026-10-02-faxbot-access-control.md`, implementing the approved whole-product refresh design.

## Global Constraints

- Dedicated installation, no new tenant platform, no authority from traits or generic key management.
- Primary owns architecture and cross-module integration; bounded workers/reviewers use Sol6.1 xhigh.
- User-facing acceptance uses actual Browser/Computer clicks and keystrokes; scratchpad bugs are fixed by Sol and replayed.
- Preserve immutable provider/attempt state and existing `key:<id>` idempotency namespaces.
- No merging/deploying unfinished slices or claiming RBAC complete from schema/CRUD.

## Review Focus

- Legacy scoped/wildcard keys and free-text owners cannot gain implicit administrator or document authority (Tasks1/2).
- A stale role/group editor cannot broaden someone else's effective access or disable the last Owner in a race (Tasks2/4).
- A revoked user with an old browser, download link, device key or open terminal cannot continue human-authorized operations (Tasks3/5).
- Lists/counts/search and individual downloads agree for unknown, renamed and moved mailbox resources (Tasks2/4).
- Session rotation preserves request replay while user disablement never resends/cancels captured accepted work (Tasks3/5).

## Files and ownership

- New `api/app/schema_access.py`, bounded `schema_checks.py` and `api/alembic/versions/0005_access_control.py`: frozen schema/upgrade, extending validation in `api/app/schema.py` and registering the revision helper in `api/alembic/env.py`.
- New `api/app/access/{__init__,types,catalog,store,policy,sessions,router}.py`: one module, internal files separated by responsibility.
- Existing `api/app/auth.py`, `main.py`, `config_values.py`, `config_store.py`, `outbound_store.py` and token/terminal/MCP adapters: primary integration only until precise ownership is delegated.
- New `api/tests/test_access_{schema,policy,store,sessions}.py`: internal module invariants. Hosted route/security contract tests are separate from primary GUI acceptance.
- New console `components/access/` views, client/types and App integration: bounded UI tasks after stable contracts.

### Task1: Frozen schema and migration

**Files:** schema_access.py,schema_checks.py,0005 migration,schema.py,alembic/env.py,test_access_schema.py,test_schema_checks.py; preserve frozen0004 tests at their explicit target revision.
**Interfaces:** `schema_access.REVISION='0005_access_control'`, `TABLES`, `frozen_metadata(*,dialect='sqlite')->MetaData`, `upgrade_access(connection,operations)->None`. Freeze exact table/column/constraint inventory in the reviewed task brief before implementation. Existing `upgrade_schema(engine)` remains the only production migration entry.

- [x] Primary and independent reviewer settled concrete metadata/legacy mapping; task brief pins15 tables,36 permissions, source-bound sessions and stable mailbox routes. No runtime auth changes in this task.
- [x] Write internal failing clean/0004 SQLite/PostgreSQL upgrades, preserved key/token/delivery rows, normalized login/role uniqueness, invalid FK/assignment/resource shapes, legacy-owner noninference and rollback/namespace conflict cases.
- [x] Implement the frozen schema, deterministic seed catalogue/builtin roles, conservative key/principal/resource migration and safe schema validation of0001–0005.
- [x] Run only migration/schema module tests locally in isolated databases; all expected cases pass without modifying existing migration files.
- [x] Independent spec/code review, exact-file commit; no claim new schema enforces routes yet.

### Task2: One policy and mutation module

**Files:** access/types.py,catalog.py,store.py,policy.py,configuration.py,__init__.py;test_access_policy.py,test_access_store.py,test_access_configuration.py.
**Interfaces:** immutable `PrincipalContext` carries principal/credential IDs, authenticated security epochs and replay scope; `ResourceRef(id)` and `AccessDecision(allowed,reason,policy_version)` carry no credentials. `AccessControl.authenticate_key(token)->PrincipalContext|None`, `authorize(context,permission,resource)->AccessDecision`, resource visibility and effective-access methods share current policy. Acceptance/replay helpers accept the caller's existing SQLAlchemy Connection and never open nested engine transactions. Mutations accept actor plus expected policy/entity versions and run in the access-state transaction.

- [x] Preparatory canonical configuration classifier implemented and independently reviewed:18 pure internal cases. Classifies scalar plus inheritance/presence changes, secret-safe results, protected/future fields require complete Owner authority. Runtime mutation integration remains pending.

- [x] Pin catalogue/builtin role membership and resource tree rules as deterministic fixtures; fail tests for no assignment, disabled subjects, credential ceiling, mixed group/direct assignments, invisible resources and malformed ancestry.
- [x] Implement authoritative current-policy reads, common visibility predicate and key/principal binding without exposing password/token hashes.
- Task 2A policy/store foundation independently reviewed, including PostgreSQL ABORT transaction-boundary correction; 137 internal SQLite/PostgreSQL cases passed. Authentication proof issuance, mutations and runtime enforcement remain pending.

- [x] Implement versioned user/group/role/assignment/key mutations with explicit typed methods; define each exact signature in the worker brief before dispatch, not a generic unvalidated command dictionary.
- [x] Prove delegation across affected role/group assignments, revoked key rotation refusal, atomic audit and two-store last-Owner/stale-editor races on both databases.
- [x] Independent review and commit of the internal mutation layer (09728046): 473 focused internal cases passed, including 179 owned cases across SQLite/PostgreSQL. Runtime integration is tracked below.
- [ ] Primary integrates an explicit operation/resource matrix, never a generic admin fallback. The current 53-route source inventory and required policy mapping are recorded in the execution scratchpad; existing HTTP enforcement remains legacy.

### Task3: Persistent login, sessions and first-owner recovery

- Session design independently reviewed. Prepared opaque token/CSRF codec implemented and independently reviewed (96 pure cases); persistent service implementation underway. Canonical bootstrap rotation already invalidates sessions atomically (9fb20c65), but the new reader is not yet wired into HTTP authentication.
- Fax resource/visibility transaction bridge independently reviewed (145 focused core/resource cases, no skips); route/acceptance integration remains pending.


**Files:** access/sessions.py,router.py;auth.py/main.py/config integration by primary;test_access_sessions.py;console login/session views.
**Interfaces:** opaque session result exposes a token only at issuance and a CSRF value; `authenticate_session(token,now)->PrincipalContext|None`; login/logout/password/reset/revoke routes from spec. Expiry12hours absolute/30minutes idle; no authority cached in token.

- [ ] Write internal failed/disabled login, reset-required restrictions, password change, expired/revoked session, cross-worker persistence and rotation tests with fixed clocks/synthetic credentials.
- [ ] Implement cookie/CSRF/Origin handling, production-auth refusal and explicit deployment-only development mode. Preserve header API-key clients and explicit key-to-session login.
- [ ] Add current identity/effective-permission console bootstrap, named-user login, visible restoration, logout/password and session management.
- [ ] Primary actual CUA login/logout/change/revoke/second-tab replay; record UI bugs, Sol repairs, replay. Hosted direct-call tests cover missing/forged Origin and CSRF separately.
- [ ] Review/commit including usable bootstrap/recovery instructions; no Users-only completion claim.

### Task4: Roles/groups/resources and full console management

**Files:** access/router.py and module mutations;main route integration;components/access/{Users,Groups,Roles,Assignments,Sessions}.tsx;client.ts/types.ts/App.tsx.
**Interfaces:** typed versioned CRUD and effective-access responses; stable resource IDs and current policy versions, no secret hashes or hidden resource names in denied responses.

- [ ] Primary enumerates every protected route and its permission/resource; annotate the source matrix with no unchecked defaults.
- [ ] Integrate resource creation atomically with outbound acceptance/inbound routing, backfill only verified ownership, and apply common visibility to list/count/search/detail/document/status actions.
- [ ] Implement all named management views with explicit scope/permission choices, safe secret-once display, pending drafts and409reload; preserve existing console styling.
- [ ] Primary real CUA grants/revokes mailbox and document access, switches named users, checks restricted lists/downloads, role/group edits and stale views; use synthetic fixture records only.
- [ ] Independent policy/route/UI review, scoped regression checks and commit. Complete documented builtin roles and migration report.

### Task5: Retained clients, high-impact operations and release proof

**Files:** terminal/tunnel/pairing/MCP/SDK/iOS adapters and docs, assigned only after interface review.
**Interfaces:** common access module decisions, session-bound one-use terminal exchange,5second idle revocation bound, user/device key ceilings; existing X-API-Key/send/status shapes retained.

- [ ] Replace host action/restart/plugin install permissions and WebSocket query secrets; enforce Origin, one-use exchange and per-input/current-policy checks.
- [ ] Bind MCP transport sessions to validated subjects; preserve explicitly scoped stdio integration mode. Implement bounded mobile pairing and revocable device keys; repair retained client permission handling.
- [ ] Prove accepted outbound lifecycle/replay independence from session/key rotation, plus prompt revocation of subsequent actions. Hosted negative direct calls supplement real available-client/GUI acceptance.
- [ ] Complete backup/restore/owner recovery, policy audit/export, versioned docs generation and exact-commit CI/browser verification. Generate an access reference from the live runtime catalogue and registered operation matrix, with exact source hashes; distinguish schema declarations from enforced routes. Maintain operator migration/Owner recovery instructions explicitly, since Autopilot's diff proposal does not rewrite them automatically.
- [ ] Audit every spec requirement, operation and client; fix gaps. Only then call complete RBAC, while retaining the whole-product provider/inbound/deployment completion gates.
