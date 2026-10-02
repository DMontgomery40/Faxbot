# Faxbot Schema Foundation Implementation Plan

**Goal:** Fresh installation and recognized historical database upgrades run through one transactional, versioned path with no lost records or artifacts. SQLite and PostgreSQL are supported and both require real verification.

**Architecture:** Preserve `init_db()` as the API lifespan entry. A schema module owns package-relative Alembic configuration, locked preflight, frozen historical schema recognition, upgrade execution and sanitized errors. Migration files own additive historical transitions. Database rebinding publishes only a migrated candidate engine while preserving the imported `SessionLocal` object. No schema changes happen just by importing models.

**Spec:** ../specs/2026-10-02-faxbot-schema-foundation.md. Independent design review: ../../modernization/evidence/2026-10-02-schema-design-review.md. Runtime prerequisite approved at c17e10c9. Initial regression evidence is retained under .superpowers/sdd/2026-10-02-faxbot-schema-foundation/.

**Execution:** Primary owns implementation and context-heavy integration; independent Sol 6.1 xhigh task reviewer receives exact fixed diff, plan brief, report and binding constraints. The preceding runtime foundation worker is the sole product writer until its report; never edit its in-progress files. Runtime review can inspect its immutable diff while primary starts isolated schema tests, but complete integration must be rerun if code changes affect runtime evidence.

## Global constraints

- Complete-product design is already user-approved; no repeated design permission required.
- Only synthetic DBs/documents, no production migration, provider credentials, real fax sends or publication here.
- Do not use create_all plus best-effort ALTER for application startup. No swallowed migration errors or blind stamp.
- Migration foundation is additive only: no drop, rename, copy/rebuild or batch recreation of existing core tables. Preserve unknown auxiliary tables, rows and references.
- Fail before mutation on unknown or incompatible core schema, unknown Alembic revision, incompatible version table, or core user triggers/rewrite rules that can intercept writes. PostgreSQL internal constraint triggers are not arbitrary user triggers.
- Keep existing job IDs/status/timestamps/provider IDs, API key hashes/scopes, tokens/expiries, mailbox/rule/event records and all artifact paths/bytes.
- Original SIP-only jobs acquire stored backend sip; never adopt the current active provider for historical work. Hybrid null fields backfill from stored backend. No resends during migration.
- Single transaction owns locked introspection, DDL, backfills and version update. Lock acquisition bounded. SQLite explicit BEGIN IMMEDIATE makes DDL transactional on Python3.11; PostgreSQL transaction advisory lock and bounded lock timeout serialize cooperating initializers.
- Validate recognized stamped schemas before no-op as well as before transitions. A version row alone is insufficient proof.
- Source/history fixtures must be independent of current model definitions; actual PostgreSQL execution is required.

## File responsibilities

- `api/app/db.py`: existing models/session factory; owned-engine construction with SQLite foreign keys; stable rebind and init_db delegation. Remove swallowed ad-hoc migration function.
- `api/app/schema.py` (new): migration runner/configuration, connection ownership and bounded locks, typed safe errors, target/head validation.
- `api/app/schema_legacy.py` (new): immutable descriptions of the supported legacy schemas and admissible optional fields/default/uniqueness variations. No mutable current Base import in historical revision decisions.
- `api/alembic/env.py`, `api/alembic.ini`: root-independent paths, percent-safe URLs, same supplied/CLI guarded connection path without recursive command.upgrade calls.
- `api/alembic/versions/0001_initial.py`: repair initial definition/recognized additive adoption without changing revision identity; constraints declared at table creation, no duplicate index or SQLite unsupported constraint alteration.
- `api/alembic/versions/0002_schema_foundation.py` (new): hybrid/backfill and additive missing compatible indexes for existing stamped0001.
- `api/tests/test_schema.py`, `api/tests/fixtures/schema/` (new): historical fixtures, preservation/rollback/concurrency/rebind/CLI tests with both actual dialects.
- `api/Dockerfile`: copy migration/config assets into runtime image after runtime task completes; preserve its coherent lock install and startup fixes.
- Makefile and CI schema-test job: real PostgreSQL repeatable verification, same upgrade command; schema is not tested only on one maintainer machine.

### Task 1: Versioned installation and historical upgrades

- [x] Promote reviewed schema contract from schema-design-notes.md and schema-design-review.md; record exact task BASE.
- [x] Red: clean SQLite CLI upgrade fails from repository root; from api it fails duplicate index. Add tests asserting correct head/schema, repeat no-op and unique identity enforcement.
- [x] Red: independent historical SQL fixtures cover original SIP fax-only, cloud fax-only with absent optional token fields, six-table pre-hybrid, current unversioned and stamped corrected0001. Insert literal synthetic records with every preserved field populated; make auxiliary CASCADE/RESTRICT child tables with rows, plus an artifact file whose hash must not change.
- [x] Freeze supported historical shapes using source commits c41a51bc,15f81951,3a480391,dd8bd991; JSON descriptors in schema-history are extraction evidence. Match each existing core table's columns/type/length/nullability/PK/defaults; accept only documented missing additions and known index differences. Recognize table sets empty, fax_jobs-only, or complete six core tables; reject incompatible partial core. Preserve unrelated auxiliary objects.
- [x] Red: incompatible unversioned schema, unknown revision, malformed/multiple version rows, head-stamped partial core, conflicting index and trigger-backfill fixtures fail with unchanged data/schema/revision. Catalog checks cover SQLite triggers and PostgreSQL user triggers/rewrite rules on core tables.
- [x] Implement additive migration chain. Original missing backend is VARCHAR(20) NOT NULL DEFAULT 'sip'; retain that server default. Admit historical absent server default or this exact literal, reject arbitrary expressions. Do not rewrite accepted existing defaults. Uniqueness may be compatible full unique index or constraint irrespective of historical name; reject partial/expression semantics as substitutes. Add missing equivalent uniqueness only after duplicate preflight in the locked transaction; never delete duplicate rows to force migration.
- [x] Red: injected mid-revision exception must roll back added columns/tables/backfill/version, then a normal retry succeeds. Implement one connection/transaction across runner and Alembic; no silent commit between adoption and revision.
- [x] Red: two subprocess initializers converge at one head; held lock yields bounded sanitized failure then retry. Use SQLite writer lock and PostgreSQL transaction advisory lock; same-process mutex protects module engine/session binding separately.
- [x] Red: every new SQLite connection enforces FKs. Same PostgreSQL URL with password does not spuriously rebind; changed URL publishes migrated candidate engine and stable SessionLocal. Failed candidate migration leaves previous working binding intact and disposes candidate. No credentials appear in outputs/errors.
- [x] Verify consistent PostgreSQL search-path/schema resolution across reflection, DDL and version table; reject ambiguous mixed core rather than silently creating another database namespace. Preserve relative SQLite URL semantics while making migration script paths package-relative. Test percent-encoded URL and CLI from root/api/unrelated working directory against same explicit absolute SQLite target.
- [x] Run real PostgreSQL tests using dedicated synthetic container at localhost65432 (private connection reference in existing scratch postgres-test.env). Create isolated test database/schema per test run and clean only owned test resources; never drop the shared test container or unrelated databases.
- [x] Update container migration assets and verify fresh plus synthetic legacy start in actual built image. Full API suite must pass on combined runtime/schema code. Add real PostgreSQL CI coverage so the proof is repeatable.
- [x] Write exact report, commit with factual subject and @codex review body, obtain independent task review, fix findings with covering regression tests, and record approved source/CLI/image evidence. Full product goal remains active.

## Integration preflight

| Producer/consumer | Contract |
|---|---|
| Runtime lifespan/schema | init_db remains sync entry; failures abort readiness/startup |
| Schema runner/Alembic | same locked Connection in attributes; env consumes without opening second transaction |
| Frozen adoption/current models | historical decisions never import changing model fields; final current head checked against current expected schema |
| Later durable jobs and RBAC/schema | new additive revisions after verified baseline; no future-column guesses now |
| SQLite/unknown auxiliary FK children | no table recreation, preserve row values and definitions |
| PostgreSQL/session rebinding | URL comparison does not stringify hidden password; validated candidate only |

## Evidence sources

`schema-design-notes.md` contains official Alembic/SQLAlchemy/PostgreSQL links; `schema-design-review.md` adds SQLite FK/ALTER and PostgreSQL search-path sources. Neither research nor design review is implementation proof. Preserve these observations in versioned engineering evidence when this task starts.

## Completed task record

Primary implementation:93be341b, review corrections26ddc26f andcbdf4489. Fixed taskBASE946438b508b9fbe6e7eb239e68c1573d8664943b; independently approvedHEADcbdf4489e5963e6aa4653ec84565da47e2b37500. No Critical/Important/Minor findings remain. Full API suites on macOS and actual Linux image with realPG16.15 each226passed,13dialect-specific skips,zero warnings; final fresh/historical/restart/preservation/refusal image proof passed. See ../../modernization/evidence/2026-10-02-schema-foundation.md and ../../modernization/evidence/2026-10-02-schema-final-review.md. No hosted CI/deployment/full-product completion is claimed.
