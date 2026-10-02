# Independent schema foundation final review

Promoted from the independent review record. Scratch filenames below refer to `.superpowers/sdd/2026-10-02-faxbot-schema-foundation/`.

**Approved fixed task range:** `946438b508b9fbe6e7eb239e68c1573d8664943b...cbdf4489e5963e6aa4653ec84565da47e2b37500` on `feat/faxbot-refresh`.

Final decision: **approve**. Remaining Critical:0. Important:0. Minor:0.

This is the final independent product/spec/code-quality review for the schema foundation slice. Original findings and intermediate decisions remain in `task-1-review.md` and `task-1-corrective-review.md`, with earlier execution evidence archived in `review-round-1/` and `review-round-2/`. The final approval supersedes their changes-required decisions for the exact final range above.

## Findings closed

1. **Invalid PostgreSQL uniqueness:** native preflight rejects invalid/not-ready/not-live indexes before the reflection-based identity checks. The guard applies to primary/version indexes as well as ordinary core indexes and executes on unversioned upgrades and stamped no-op paths. Real failed concurrent unique-index regression covers duplicated key IDs and atomic refusal. Saved original reproduction independently replayed on corrected code now refuses safely.
2. **SQLite conflict policies:** native core/version DDL keyword scanning rejects nonhistorical `ON CONFLICT` policies, including comment-separated keywords. The scanner ignores quoted strings/identifiers and comments; `index_xinfo` adds native checks. Replace/ignore/commented-replace regressions cover unversioned and stamped cases with populated CASCADE children preserved on refusal. Saved replacement-policy reproduction now refuses safely.
3. **Changed comparison/index semantics:** native SQLite column/index collation checks and PostgreSQL column/index collation/operator-class/state checks reject collated, deferred, exclusion, partial/expression, include, ordering, and nonhistorical access semantics. Real nondeterministic ICU column/index and SQLite NOCASE reproductions are covered; saved collation reproductions now refuse safely. The second-round custom-default-class gap is closed by requiring exact historical built-in `pg_catalog.text_ops`, rather than only `opcdefault`. Every frozen core/version index key is VARCHAR, so this precise identity matches the supported baseline. The actual custom DEFAULT varchar casefold class was added as a RED-first regression; I independently replayed it at final HEAD and confirmed safe refusal. Output: `review-default-opclass-corrected.txt`.
4. **CHAR and timestamp type compatibility:** reflected VARCHAR/Text types now require the supported exact classes. Fixed CHAR is refused on both dialects; the saved PostgreSQL padded-backend reproduction now refuses safely. PostgreSQL timezone/reduced timestamp precision is rejected without rewriting old values.

No additional substantive standards or correctness issue was found in the corrective or final diffs. The full task implements the approved additive migration design: frozen independent historical definitions/fixtures, repaired retained0001 plus additive0002, validated known version states, one bounded locked transaction, original SIP/default and stored hybrid-provider behavior, auxiliary row/artifact preservation, stable imported SessionLocal and validated candidate rebinding, working-directory-safe CLI, runtime migration assets, and repeatable real PostgreSQL CI coverage. No existing core table rebuild/drop/copy path was introduced, and the existing startup entry continues to propagate failure before readiness.

## Final source and execution reconciliation

Verified final HEAD and no uncommitted product changes in `api`, Makefile, or CI against `cbdf4489e5963e6aa4653ec84565da47e2b37500`. Read final `docker-build-metadata.json`, `full-api-linux-image.txt`, `docker-schema-proof.json`, and per-case startup/shutdown logs. Their source/image identity agrees:

- Source: `cbdf4489e5963e6aa4653ec84565da47e2b37500`.
- Actual API Linux/arm64 image: `sha256:b0360e7953b9ef7d5afb8388604e8aa6d2df359b4446ad243b8d2f35af0c3d85`.
- Final macOS full API suite:226passed,13dialect-specific SQLite-parameter skips,27.47s,zero warnings.
- Final full API suite inside the actual image with real PostgreSQL16.15:226passed,13same skips,24.19s,zero warnings. Every PostgreSQL-only counterpart executes; these are not unavailable-PostgreSQL skips.
- Actual image entrypoint: fresh, original SIP `c41a51bc`, and full pre-hybrid `3a480391` startup/restart/readiness all succeed at version0002. Every seeded old field, artifact hash, and auxiliary child survives both starts. Authenticated historical job reads returnHTTP200. Logs show startup and shutdown completion; valid cases terminate143 after clean shutdown.
- Incompatible synthetic core startup exits3 before startup completion, with no version stamp or mutation. Its sanitized unsupported-schema error is present in the actual image log.

Existing rollback, held-lock/retry, subprocess convergence, stable binding/concurrent rebind, percent-safe multi-cwd CLI, optional-column, uniqueness-equivalence, and foreign-key proof were inspected in prior rounds and remained covered by the final combined suite. I used focused owned synthetic reproductions for concrete catalog doubts and did not redundantly rerun the full suite.

Standards axis:0 remaining substantive findings. Spec/correctness axis:0 remaining findings. Approval is for this exact schema foundation implementation and its recorded local/actual-image evidence; no hosted CI, production migration, provider call, deployment, or full modernization completion is claimed. All reviewer product access was read-only; synthetic PostgreSQL namespaces were removed and credentials were never printed.
