# Faxbot schema foundation design

## Verified source history

- c41a51bc: only fax_jobs, original nine fields (id,to_number,file_name,tiff_path,status,error,pages,created_at,updated_at). These jobs were SIP; safely backfill backend=sip on adoption, not current deployment default.
- 15f81951 / d6f5a959: fax_jobs with backend,provider_sid,pdf_url,pdf_token,pdf_token_expires_at; ad-hoc token addition may have run independently. Tokens are optional known additions.
- 3a480391: all six present models, before hybrid outbound_backend/inbound_backend fields.
- dd8bd991 and main: all six models with optional hybrid fields. Unknown auxiliary tables from feature branches must be preserved, never dropped during adoption.
- 0001_initial: broken duplicated fax_jobs.to_number index; SQLite-unsupported post-create inbound_events uniqueness; no hybrid fields; missing several nonunique model indexes. Existing stamped 0001 installations still need an additive 0002 revision.
- Current URL comparison str(engine.url) masks PostgreSQL passwords, so it will rebind needlessly. Compare URL objects via make_url, dispose old engine only after replacement is validated/ready; preserve imported SessionLocal identity.

## Migration contract

Keep init_db() as the startup entry for caller compatibility, but delegate to one versioned migration runner. Importing models must not connect or mutate the schema. Support SQLite and PostgreSQL only; no best-effort MySQL branch.

Use package-relative Alembic paths independent of working directory; include migrations in actual Docker image. Env accepts supplied Connection in Config.attributes; avoid ConfigParser interpreting percent-encoded URLs (set escaped option or pass connection directly). Existing `alembic -c api/alembic.ini upgrade head` and Makefile entry must reach the same locking/preflight machinery as startup. Do not recursively invoke command.upgrade from env.py.

Freeze the original schema/adoption definitions as migration-owned metadata; never import future mutable Base.metadata to decide historical revisions. Repair 0001 for fresh creation; add 0002 to normalize supported legacy/current records and add/backfill hybrid fields. Keep published revision identity rather than squashing history. A legacy database with no revision is validated before any DDL, then brought through the explicit chain; never simply stamp an arbitrary schema head.

Recognized unversioned shapes: zero Faxbot tables; original fax_jobs-only; cloud fax_jobs-only; complete six-table core before/after known optional columns. Validate every existing core table (column names/types/lengths/nullability/primary key/unique constraints), accepting only documented optional-field and historical index differences. Any other partial core, unknown core columns, incompatible types/nullability, ambiguous constraints, conflicting index definition or unknown Alembic revision fails before mutation with a sanitized actionable error. Preserve unknown auxiliary tables and their rows. A database with auxiliary tables but no Faxbot core may install core only if names do not conflict; never touch auxiliary data.

Original SIP-only jobs receive backend=sip; absent cloud IDs/URLs/tokens become null. Existing backend and effective hybrid values stay intact; backfill null hybrid fields from stored backend only. Preserve all IDs, statuses, timestamps, hashes, key scopes, tokens/expiries, mailbox labels, event identities and artifact paths. Do not resend or rewrite documents during migration. Unique identity constraints must already hold or migration fails atomically; do not resolve duplicates by deletion.

Serialize the entire inspection+DDL+version update in one transaction. SQLite Python3.11 requires explicit BEGIN IMMEDIATE before DDL to actually make DDL rollback and prevent concurrent startup races; acquire lock with bounded timeout. PostgreSQL uses a fixed application migration transaction advisory lock and bounded lock timeout; restore transaction-local settings at commit automatically. Pass the same Connection to Alembic. Unknown revision and preflight failures leave rows/schema/revision unchanged. Test real injected mid-migration failure and subsequent retry.

Enable SQLite foreign_keys on every application-created connection before transactions, for upcoming RBAC/worker referential constraints. No global SQLAlchemy hook affecting unrelated engines; install on owned engine. Main startup must propagate schema failure rather than serving ready. Runtime lifespan will call unchanged init_db entry; avoid touching lifecycle concurrently.

## Required behavioral proof

1. Fresh SQLite and PostgreSQL upgrade head yields current core schema and explicit version; repeat is no-op; uniqueness enforced.
2. Populate independent SQL fixtures for original SIP, cloud-only (token fields optional), complete pre-hybrid and current unversioned schemas; migrate and compare all previous rows and artifact bytes. Use literal fixtures derived from historical revisions rather than current models to avoid testing implementation with itself.
3. A stamped corrected-0001 fixture upgrades through 0002, preserving all values.
4. Unknown revision, partial/incompatible core, duplicate/conflicting index or constraint fails without schema/data/revision changes; unknown auxiliary tables survive success.
5. Two subprocess initializers against same file/Postgres DB converge on one head; held lock causes bounded sanitized failure then retry succeeds.
6. Percent characters/encoded credentials in DB URL work without interpolation or credential output. Same target with password does not rebind; changed URL updates imported SessionLocal safely.
7. Foreign keys enabled across separate connections. Test a deliberately invalid reference on a synthetic auxiliary FK table without requiring RBAC implementation.
8. CLI from root, api and unrelated cwd works; application startup and actual Docker image clean install/legacy upgrade use same path.
9. Full API suite passes; real PostgreSQL target mandatory, not only compilation of PostgreSQL DDL.

## Primary references consulted

- https://alembic.sqlalchemy.org/en/latest/cookbook.html#sharing-a-connection-across-one-or-more-programmatic-migration-commands
- https://docs.sqlalchemy.org/en/20/dialects/sqlite.html#enabling-non-legacy-sqlite-transactional-modes-with-the-sqlite3-or-aiosqlite-driver
- https://docs.sqlalchemy.org/en/20/dialects/sqlite.html#foreign-key-support
- https://www.postgresql.org/docs/16/explicit-locking.html#ADVISORY-LOCKS

## Scope boundary

Configuration/provider activation is a distinct next task; do not combine it into this migration review. Durable jobs and RBAC build additional additive revisions after the foundation passes. No deployment or production data touched by this research.

## Independent design review resolved

Read the independent design review findings below before implementation. Primary accepted both P1 design corrections: (1) strictly additive foundation changes with no DROP/RENAME/batch recreation of core tables, adding absent original backend as VARCHAR(20) NOT NULL DEFAULT 'sip' and retaining that default; (2) reject nonhistorical user triggers/rewrite behavior on core tables before backfill. Add CASCADE/RESTRICT auxiliary-child preservation and trigger rejection fixtures. Default and uniqueness equivalence are explicitly whitelisted, not normalized by destructive rewrite. Also validate stamped 0001/head schema and version-table shape, same-process binding serialization, and same target schema throughout PostgreSQL introspection/DDL. Review approves amended design direction, not implementation.
