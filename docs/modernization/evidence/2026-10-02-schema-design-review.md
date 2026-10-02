# Schema foundation design review

Date: 2026-10-02. Bounded, read-only review of the proposed next migration slice. No product source or database was changed. This report is a design review, not implementation approval or release evidence.

## Verdict

The migration direction is sound. Two preservation gaps in the original notes needed explicit corrections before implementation. The primary confirmed both corrections during this review: **use additive changes only, retaining the new legacy `backend` server default**, and **reject unknown user triggers on core tables before backfill**. Proceed with the amended contract below; verify the resulting implementation independently.

## Findings and smallest corrections

### 1. [P1] SQLite table recreation can silently delete auxiliary rows

Design references: `schema-design-notes.md:20`, `:22`, `:24`, `:28`. Historical evidence: `schema-history/c41a51bc.json` has a populated `fax_jobs` table without the required non-null `backend` column; later descriptors require that column. The notes promise preservation of unknown auxiliary tables while enabling SQLite foreign keys, but originally did not specify how to enforce the added non-null column or normalize constraints.

A tempting implementation is add nullable `backend`, fill it, then use Alembic batch recreation to enforce nullability or remove a temporary default. Batch recreation drops the old parent table. With SQLite foreign keys enabled, `DROP TABLE` executes an implicit delete and may invoke foreign-key actions. An auxiliary table referencing `fax_jobs(id) ON DELETE CASCADE` can therefore lose its rows while the recreated core table still contains every fax job. A successful commit and even a clean `foreign_key_check` do not detect the lost children. A RESTRICT reference instead blocks the upgrade. Switching `PRAGMA foreign_keys=OFF` after `BEGIN IMMEDIATE` is ineffective. [SQLite foreign-key behavior](https://www.sqlite.org/foreignkeys.html#fk_actions), [Alembic batch foreign keys](https://alembic.sqlalchemy.org/en/latest/batch.html#dealing-with-referencing-foreign-keys).

**Correction confirmed by primary:** No drop, rename, or batch recreation of existing core tables in this foundation slice. Add an absent original-SIP `backend` as `VARCHAR(20) NOT NULL DEFAULT 'sip'` and retain that literal server default. Add the optional cloud/hybrid columns directly; create only absent historical tables and additive indexes. SQLite permits a non-null added column when its default is non-null. The review environment uses SQLite 3.47.1, SQLAlchemy 2.0.34, and Alembic 1.13.2; do not rely on newer SQLite ALTER COLUMN support. [SQLite ADD COLUMN restrictions](https://www.sqlite.org/lang_altertable.html#altertabaddcol).

Add preservation fixtures with an auxiliary child table using CASCADE and another using RESTRICT, both populated before upgrade. Compare their rows and FK definitions afterward. A standalone invalid-reference probe proves FK enforcement, but does not prove upgrade preservation.

### 2. [P1] Column/constraint validation alone permits backfill-triggered changes to preserved values

Design references: `schema-design-notes.md:22` and `:24`. The specified preflight checks names, types, lengths, nullability, keys, and uniqueness, then updates existing rows to backfill hybrid provider columns. None of the four historical descriptors contains a user trigger.

A database with the exact recognized columns and indexes can also have an `AFTER UPDATE` trigger on `fax_jobs` or `inbound_faxes`. The hybrid backfill can fire it, changing `updated_at`, provider identities, status, or auxiliary audit rows. That violates the preservation promise even if every explicitly checked schema field matches and the migration commits normally. Accepting such a database is an unknown-schema guess.

**Correction confirmed by primary:** Inspect database catalogs for user triggers attached to core tables and reject them, with a sanitized table/object-specific diagnostic, before any DDL or backfill. Exclude PostgreSQL internal constraint triggers. Apply the same fail-closed reasoning to user rewrite rules or other nonhistorical core behavior that would intercept the migration writes; do not disable/drop/reinstall unknown behavior as an adoption shortcut. Extra triggers on unrelated auxiliary tables need not be rejected when the foundation never writes to those tables.

Add one otherwise valid historical fixture with a trigger that changes `updated_at` and inserts an auxiliary audit row. Upgrade must fail without changing schema, revision, timestamps, or audit rows.

## Amended compatibility details to preserve

These are narrow implementation requirements, not additional redesign requests:

- Treat defaults as an explicit whitelist, not a reason to make all accepted databases structurally identical. Existing historical columns have no database server defaults. For `fax_jobs.backend`, admit the historical absent default and the new migration-owned literal `sip` default; retain the existing accepted definition. Do not strip the `sip` default on retry or a later adoption pass. Reject arbitrary expression defaults that are outside the frozen definitions. Never copy/cast old values solely for length or default parity.
- Treat required uniqueness as logical equivalence. `api_keys.key_id` is a unique index in the models; mailbox uniqueness is an unnamed model constraint but `ux_mailboxes_label` is an index in 0001; inbound-event identity is a composite constraint. Preserve a compatible existing full unique index/constraint even when its name differs. If a required identity is missing, primary permits adding an equivalent unique index only after duplicate preflight in the locked transaction. A partial, expression, or otherwise semantically different index is not equivalent. Keep the existing constraint when it is already sufficient.
- Freeze and validate admissible shapes for **unversioned, 0001, and head** states. An existing known version row is not sufficient evidence that its core tables have the expected schema. Validate before the no-op path as well as before migrations, and require the version table itself to have the expected shape and one supported linear revision. A head stamp on a partial core must fail without mutation.
- Make the legacy transition explicit: validated unversioned original/cloud fax-only databases retain `fax_jobs`; 0001 establishes only the missing base structures/columns; 0002 adds/backfills hybrid columns and missing compatible indexes. A complete pre-hybrid/current core skips creation of existing structures. Avoid `IF NOT EXISTS` or `create_all` as a substitute for shape validation. All inspection must use the connection holding the lock.
- Keep native PostgreSQL schema resolution consistent across preflight, DDL, and version-table operations. The installed SQLAlchemy PostgreSQL dialect uses `pg_table_is_visible` for reflection with `schema=None`, so a generic claim that this reflection only sees `public` would be incorrect. If an explicit target schema is introduced, use it consistently and reject mixed/ambiguous core resolution instead of silently creating a new core. [PostgreSQL search-path semantics](https://www.postgresql.org/docs/16/ddl-schemas.html#DDL-SCHEMAS-PATH).
- Cross-working-directory proof should use the same explicit database target, including an absolute SQLite file URL. Package-relative Alembic script lookup does not change the existing semantics of `sqlite:///./faxbot.db`; do not silently reinterpret that URL relative to a newly chosen directory.

## Transaction and concurrency assessment

The proposed explicit SQLite `BEGIN IMMEDIATE`, bounded PostgreSQL transaction advisory lock, shared Alembic connection, and external transaction ownership are appropriate. Keep one outer transaction from locked preflight through the final version update; CLI and startup must enter the same guarded execution path. Alembic's documented shared-connection pattern supports that structure. [Alembic connection sharing](https://alembic.sqlalchemy.org/en/latest/cookbook.html#sharing-a-connection-across-one-or-more-programmatic-migration-commands).

The runner must roll back on any revision failure and dispose only a failed candidate engine when rebinding. Publish the candidate engine and update the existing `SessionLocal` binding only after the migration commits successfully; imported session-factory identity remains stable. A same-process serialization guard is a small addition if concurrent initializers can change the module's binding, because the database lock serializes DDL but does not serialize Python global assignment.

The original required behavioral proofs remain necessary. Add the two preservation probes above and a known-head-but-incompatible-schema rejection probe. Neither this report nor the design's transaction statements proves rollback, real PostgreSQL upgrades, subprocess convergence, or the Docker/CLI path; those remain implementation evidence requirements.

## Sources reviewed

- Primary `schema-design-notes.md`, then all four independent JSON schema descriptors and their stated source commits.
- Approved `docs/superpowers/specs/2026-10-02-faxbot-refresh-design.md` upgrade/data-preservation constraints.
- Existing `api/app/db.py`, `api/alembic/versions/0001_initial.py`, `api/alembic/env.py`, `api/alembic.ini`, configuration URL defaults, and Makefile migration entries.
- Historical `api/app/db.py` at `15f81951` and `d6f5a959`; installed PostgreSQL reflection implementation and dependency versions; authoritative SQLAlchemy/Alembic/SQLite/PostgreSQL references linked above.

No production data, credentials, runtime-lifecycle source, provider configuration, or RBAC design was modified or expanded by this review.
