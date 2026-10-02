# Faxbot refresh progress

## Goal

Completed and verified product by October 7, 2026, 10:00 AM Mountain. Keep the familiar UI as a starting point; upgrades are permitted. Complete RBAC is required. Preserve compatibility with the existing TestFlight iOS app. No demo-plus-backlog interpretation. Preserve Docs Autopilot / OpenAPI / Redocly / MkDocs / Mike and make updates run on every main commit as requested.

## Execution

- Integration worktree: `/Users/davidmontgomery/.codex/worktrees/faxbot-refresh/Faxbot`.
- Integration branch: `feat/faxbot-refresh`.
- Base: `7e0c15fa` (matches fetched origin/main).
- Goal mode: active; not complete.
- User-selected worker model: `gpt-6.1-sol`, reasoning effort `xhigh`.
- Primary agent owns architecture, state/recovery design, complex cross-module changes and context-heavy decisions. Bounded implementers and independent reviewers receive sufficient context through task briefs and evidence pointers.
- First implementation cycle started after explicit plan approval. No production deployment yet.

## Completed discovery

1. Initial main assessment: `initial-assessment.html`. The original temp report was visually checked in a browser. Its findings apply to main; newer branches require reconciliation.
2. Isolated main baseline: 27 Python tests passed in disabled mode. Real conversion and provider delivery were not covered.
3. Direct content probe: normal.txt produced a 1,571-byte PDF; contest.txt produced a 19-byte placeholder with FAX_DISABLED=false.
4. Fetched and inspected remote branch identities. development, iOS, desktop and docs contain material work absent from main.
5. Installed only the additional Matt Pocock workflow skills needed for evaluation/execution: wayfinder, implement-spec, to-spec, to-tickets, implement, tdd. Source pinned to mattpocock/skills commit d81f3a183412e71a5b1e84ca21bc1a35eea03a60; preexisting skills preserved.
6. Two Sol 6.1 xhigh read-only inventory agents mapped contracts and deployment/docs. Their completed reports will be linked alongside this file.

## Design review and next implementation plan

`../superpowers/specs/2026-10-02-faxbot-refresh-design.md` is a proposed concrete design, opened to the user. An asynchronous question requests written-design approval, especially dedicated self-hosted deployment, retained workflows, and test interfaces. User replied with three clarifications: RBAC must be finished, UI upgrades are welcome (the prior UI is liked but not frozen), and the iOS app exists in TestFlight and is fine-ish. Those changes are incorporated in the written design. No deployment-model change was requested. User explicitly approved the revised design and first implementation plan: "Yes—execute with the agreed agent split". Begin execution; do not request this approval again.

The concrete task graph and first document-integrity plan are versioned. A Sol 6.1 xhigh implementer owns Task 1 conversion, followed by independent review. The primary owns upload/API integration decisions and Task 2. Do not redispatch completed inventory work after compaction.

## Current implementation evidence

- Worktree `.venv`: Python 3.11 with baseline API requirements installed.
- Real Ghostscript 10.08.0 is available for PDF-to-TIFF verification; installation completed successfully.
- Dedicated Colima profile `faxbot-refresh` started successfully. Explicit Docker context `colima-faxbot-refresh` reports Docker 28.4.0 on Linux/aarch64. A dedicated PostgreSQL 16.15 test container is available on loopback port 65432 with synthetic data. The initial application image exposed the dependency conflict described below; the runtime and schema slices subsequently repaired and verified actual images. No production deployment is claimed.
- Task 1 implementation base: `c2d169e6`. Brief, report and task review records live in the ignored `.superpowers/sdd/2026-10-02-faxbot-document-integrity/` directory during execution. Final evidence is promoted here.
- A read-only DNS/health probe of `https://api.faxbot.net/health` could not resolve that hostname. This does not identify the intended backend or establish that it is down. The user has been asked for the actual deployment URL/location or existing access reference while independent implementation continues.
- Historical pre-repair fresh-database migration probe: `python -m alembic upgrade head` against temporary SQLite fails in `0001_initial` because `ix_fax_jobs_to_number` is declared twice. That RED result drove the schema foundation slice; final implementation and verification are recorded below.
- Existing Admin UI baseline: `npm ci && npm run build` succeeds. Audit reports 12 dependency findings (1 critical, 8 high, 2 moderate, 1 low), with the critical finding in Vitest. Audit details are preserved in the scratch evidence directory for the required dependency slice. Build also warns about Vite's CJS API and the large application bundle. No automated force upgrade was applied.
- GitHub read-only access check confirms repository administration/push rights and configured secret names for docs deployment, website deployment, OpenAI and package registries. Secret values were not read. Presence of a secret name is not proof the credential works.

## Important discovered constraints

- Main's docs jobs do not implement the requested every-main-commit behavior. Docs Autopilot automatically creates plans on development; later API generation was disabled; Mike publishes the mkdocs branch. Restore actual generation and publication rather than hand-editing generated output.
- Current Node MCP tools have a duplicate default export that fails parsing.
- Mobile source exists on origin/iOS. Do not label it missing merely because it is absent from main.
- Existing client, provider, tunnel, pairing, migration and distribution claims require the explicit capability inventory before implementation sequencing.
- External provider accounts, controlled destinations, signing/distribution access, actual deployment identity, and container runtime availability must be established before their release gates can pass. No fabricated or simulator-only operational success.

## Skill/process choices

Matt's implement-spec supplies the whole-spec task-graph pattern; Superpowers SDD supplies bounded briefs and per-task independent review. Do not run two competing orchestration loops. Start with one implementation writer at a time while the monolithic backend is shared; parallelize read-only discovery/review and later only genuinely isolated work. Keep the user's explicit model choice over generic skill tier advice. Primary handling of context-heavy work follows the user's instruction.

Wayfinder is available for unresolved large decisions; do not make a planning-only loop the deliverable. Completion means implementation, validation, release and transferability evidence, not closed planning tickets.

## Document integrity complete; runtime foundation next

- Conversion, upload integration and the whole bounded slice are independently approved at `6a0afbba`; no remaining Critical/Important document findings.
- Final API suite: **109 passed**, zero skips, four pre-existing lifecycle warnings. Final local HTTP proof preserves all 131 original lines across three PDF pages and three real TIFF frames; malformed input and unauthorized downloads fail correctly.
- Evidence: [document-integrity verification](evidence/2026-10-02-document-integrity.md).
- Actual production Dockerfile build uncovered a release blocker: separate MCP installation upgrades Starlette to 1.7.0 while FastAPI 0.112.2 requires <0.39. `pip check` and API import fail in the built container. Next: resolve a supported common API/MCP dependency set, supported lifecycle and actual container startup, then versioned schema/configuration, durable jobs and complete RBAC.
- This is a completed subsystem, not a deployed or finished product. The full goal remains active.

## Runtime foundation complete; schema upgrades next

- Runtime source/image implementation a8ca0ac9 and CI-only follow-up c17e10c9 are independently approved, with no Critical/Important findings.
- Fresh macOS and real Linux image suites:125 passed each, zero warnings. Both actual Docker images start, pass package checks and shut down cleanly. Authenticated document contents, real TIFF output, installed Python package entrypoints and MCP HTTP/SSE initialization are verified. Primary separately exercised MCP send/status through the API and downloaded the preserved original text.
- Evidence: [runtime foundation verification](evidence/2026-10-02-runtime-foundation.md). Hosted CI/deployment and real fax delivery remain separate gates.
- Minor for provider/diagnostics and final review: add safe categories to AMI reconnect warning messages; no credential/error-content leakage. Existing UI audit findings remain mandatory UI/dependency work.
- Primary completed schema architecture and independent design review. Four historical shapes create successfully as test fixtures on both SQLite and actual PostgreSQL16.15. New upgrade regressions reproduce three failures at c17e10c9: cwd-dependent paths (root/unrelated folder) and duplicate index creation (api folder). This is RED evidence, not a completed migration.
- Next: implement the reviewed additive versioned migration path, preserving historical values, auxiliary references and files. Then provider configuration, durable delivery and full RBAC/client/docs/release gates proceed under the same active goal.

## Schema foundation complete; provider configuration next

- Primary implementation and independent Sol6.1 xhigh review are approved through source `cbdf4489`; no Critical, Important or Minor findings remain. All review findings have corrective code and regression evidence; final review reconciles exact source/image/startup evidence.
- Startup and CLI now share explicit versioned, additive, locked, transactional upgrades. Historical rows/artifacts/auxiliary references survive; unsupported schemas cannot be blindly stamped or served as ready. Engine rebinding preserves stable SessionLocal and only publishes a successfully upgraded candidate.
- Full API suites on macOS and actual Linux API image:226 passed each, zero warnings. Thirteen skips are PostgreSQL-only cases under the SQLite parameter; all actual PostgreSQL counterparts execute and pass on PostgreSQL16.15. CI now provisions PostgreSQL.
- Actual committed API image `sha256:b0360e7953b9ef7d5afb8388604e8aa6d2df359b4446ad243b8d2f35af0c3d85` passes fresh and historical startup/restart/readiness, original row/file/auxiliary-child preservation, and incompatible-schema startup refusal. Source `cbdf4489e5963e6aa4653ec84565da47e2b37500`. Owned proof containers/volumes removed; synthetic PostgreSQL container retained for upcoming work.
- Evidence: [schema foundation verification](evidence/2026-10-02-schema-foundation.md). First review found invalid-index, SQLite conflict-policy, custom-collation and CHAR-type gaps; subsequent review found a custom default PostgreSQL comparison-class gap. All reproduced and corrected; earlier reviews/evidence retained.
- Next: primary design/implementation of provider configuration and activation. Source inspection confirms cwd-dependent paths, invalid-provider fallbacks, in-place partial settings mutation, stale cached credentials, manifest dispatch through legacy rather than accepted provider, and plugin settings that persist without activating. The detailed next-step source notes are in the schema scratch directory; do not rediscover the entire repository or repeat user approval.
- Full modernization goal remains active. No production migration, deployment, real fax delivery or finished-product claim is implied by this subsystem checkpoint.
