# Schema foundation verification

The versioned installation/upgrade implementation is complete at `cbdf4489e5963e6aa4653ec84565da47e2b37500`; independent review is approved with no remaining Critical, Important or Minor findings. See [final review](2026-10-02-schema-final-review.md). This is a bounded subsystem checkpoint, not release/deployment approval.

## Source and implementation

Task base: `946438b508b9fbe6e7eb239e68c1573d8664943b`.
Initial implementation: `93be341bd08bada935a5bb4019cf6909fd264e67`.
Review corrections: `26ddc26f5cf740ba2f8cb149923f0b60a62eec6c` and `cbdf4489e5963e6aa4653ec84565da47e2b37500`.

`init_db()` now uses one guarded Alembic upgrade path, also used by the CLI. The broken `0001_initial` identity is retained and repaired; additive `0002_schema_foundation` adds effective backends and missing compatible indexes. Frozen migration definitions and independent historical SQL fixtures come from source revisions c41a51bc,15f81951,3a480391,dd8bd991. Historical decisions do not import mutable ORM models.

Core tables are never copied, dropped, renamed or rebuilt. Original SIP-only jobs receive stored backend `sip`; missing effective backends backfill from each row's stored backend, while populated values remain unchanged. Existing IDs, statuses, timestamps, provider IDs, API key hashes/scopes, tokens/expiries, mailbox/rule/event fields, artifact paths and bytes remain intact. Unknown auxiliary rows and CASCADE/RESTRICT foreign-key references survive.

The runner validates recognized unversioned and stamped schemas before writes. It checks namespace, table set, columns/types/nullability/defaults/primary keys, identity uniqueness, index semantics, and migration version shape. Unknown revisions, incompatible partial core, duplicates, core user triggers/rewrite rules, row-security/inheritance, custom comparison/conflict semantics and invalid PostgreSQL indexes are rejected. A version stamp alone is never sufficient.

SQLite uses explicit `BEGIN IMMEDIATE`, bounded lock acquisition and foreign keys on every owned connection. PostgreSQL uses a transaction advisory lock with transaction-local lock timeout. The same connection/transaction owns preflight, DDL, backfills, revision updates and final validation. Failed migrations roll back, and retries can succeed. Engine replacement is serialized within the process: a candidate must migrate before publishing; the imported SessionLocal object remains stable, failed candidates are disposed, and the previous binding survives failure. URL object comparison retains password identity.

CLI paths are package-relative and accept percent-encoded environment URLs. The actual Dockerfile now includes migration assets. CI provisions PostgreSQL16 alongside the API suite. `make test-schema` requires an explicit dedicated PostgreSQL test URL.

## Verification on final source

| Environment / check | Result |
|---|---|
| macOS Python3.11, full API suite plus real PostgreSQL16.15 | 226 passed,13 dialect-specific skips,27.47s; zero warnings |
| Actual built Linux API image, full same suite plus real PostgreSQL16.15 | 226 passed,13 dialect-specific skips,24.19s; zero warnings |
| Fresh SQLite CLI from repository/api/unrelated cwd | Correct head; percent-containing filename supported |
| PostgreSQL CLI from unrelated cwd | Percent-encoded connection options work; correct isolated schema |
| Historical shapes and corrected stamped0001 | Old values/files/auxiliary children preserved; repeated upgrade no-op |
| Injected failure after DDL/backfill | Columns/tables/rows/version roll back; retry succeeds |
| Two subprocess initializers and held database lock | Concurrent startup converges; lock failure bounded; retry succeeds |
| Same/changed/failed engine target | Stable session factory, no password-driven false rebind, safe disposal/publication |
| Incompatible schema and catalog variants | Rejected atomically, including already-stamped/no-op cases |

The thirteen skips are PostgreSQL-only cases under the SQLite parameter; their PostgreSQL counterparts ran and passed. No real-PostgreSQL coverage was skipped for missing access. Both environments used synthetic data and isolated test namespaces.

## Actual deployment image proof

- Source: `cbdf4489e5963e6aa4653ec84565da47e2b37500`, exported by `git archive`; no working-tree secrets/data included.
- Image: `faxbot-refresh:schema-cbdf4489e596`.
- ID: `sha256:b0360e7953b9ef7d5afb8388604e8aa6d2df359b4446ad243b8d2f35af0c3d85`.
- Platform: Linux/arm64, actual `api/Dockerfile`, explicit Docker context `colima-faxbot-refresh`.

Fresh startup and seeded original SIP-only/full pre-hybrid startup reached readiness. Each restarted successfully with the same rows and artifacts. Authenticated historical job reads returned HTTP200. The original SIP fixture retained three rows including auxiliary children; the full-core fixture retained eight. Preserved synthetic artifact SHA256: `e3b3ddade2482f93b226b867e119377d0fc218c9e3784f64bf78cf927b02a65a`.

An incompatible fax_jobs schema exited3 before application startup completed and retained its schema/data without a version stamp. Healthy instances shut down cleanly (Uvicorn/tini exit143) without warnings/errors, unfinished-task messages or OOM. All owned proof containers and volumes were removed. No external fax was sent.

The proof harness initially reused an ephemeral Docker port across restart. Reading the restarted container's assigned port corrected that harness assumption; it required no product-source change.

## Independent review corrections

The first independent Sol6.1 xhigh review found four Important issues, all accepted and corrected:

1. PostgreSQL failed concurrent unique-index builds could be mistaken for enforced uniqueness. Native validity/readiness/liveness and semantic checks now reject them, including on head-stamped databases.
2. SQLite custom conflict rules could silently replace records. Native DDL validation now rejects those rules before adoption; CASCADE-child preservation is tested.
3. Custom SQLite/PostgreSQL collations could change identity comparisons. Native column/index checks reject them, including actual PostgreSQL nondeterministic ICU casefold collations and alternate operator classes.
4. CHAR could pass as VARCHAR and return padded provider keys. Supported reflected types are now checked precisely; reduced timestamp precision is also rejected.

A subsequent focused review found that a custom PostgreSQL operator class can label itself default while changing equality. Final validation requires the exact historical built-in pg_catalog.text_ops identity; the real custom-class reproduction fails before the fix and is rejected afterward. The first review-derived regression run reproduced eleven failures before the corrections. Final source includes negative tests and unchanged-row/schema assertions. Catalog checks follow [PostgreSQL pg_index](https://www.postgresql.org/docs/16/catalog-pg-index.html), [SQLite index_xinfo](https://www.sqlite.org/pragma.html#pragma_index_xinfo), and [SQLite conflict behavior](https://www.sqlite.org/lang_conflict.html).

## Evidence and remaining whole-product gates

Detailed scratch evidence is retained in `.superpowers/sdd/2026-10-02-faxbot-schema-foundation/`: exact task/corrective reports and diffs, independent review and repros, final suite logs, Docker build metadata, per-case startup/shutdown logs, JSON assertions and replay scripts. Earlier rounds remain in `review-round-1/` and `review-round-2/`.

Hosted CI has not run and no production migration/deployment/PR/merge is claimed. Provider configuration, durable delivery/receipt, complete RBAC, retained clients, documentation automation, security/dependency review and real controlled fax/release/handoff evidence remain required under the active whole-product goal.
