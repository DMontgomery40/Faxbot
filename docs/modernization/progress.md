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

## Provider paths complete; configuration values/activation in progress

- Resource paths are independently approved at `48c9ff96`;32 focused and258 full API tests pass (13 expected dialect skips), zero warnings. Actual committed Linux image `sha256:a6a6974b5e7887f51bda7caac25c9929a95a7a757ebd8b871aa0639605287106` verifies configured manifest identity across installation, document acceptance, dispatch and refresh from `/tmp`, with no external network. Evidence: [provider paths](evidence/2026-10-02-provider-paths.md).
- Review caught and corrected source bundle shadowing and escaped-directory backlink cases before acceptance. Actual image validation also reproduced the separately owned stale readiness provider-list defect after plugin installation; it remains required activation work. Plain-JSON bundled scrape parsing and cache activation also remain explicitly tracked.
- Primary selected and independently challenged the concrete configuration architecture in `../superpowers/specs/2026-10-02-faxbot-configuration-activation.md`: canonical immutable DB revisions/profile bindings, durable Apply, complete literal exports, atomic configuration/job selection, and truthful whole-patch restart staging. Review clarifications are incorporated. No additional user design approval is needed.
- An isolated baseline probe demonstrates current full export drops hybrid Sinch/SignalWire and inactive Documo credentials, persisted parsing mishandles escaped newlines and mutates unrelated process environment. These are the values/persistence regression targets.
- Primary has begun the typed immutable values model/tests; a bounded Sol6.1xhigh worker owns only the literal environment-file codec and its tests. These new modules are not yet integrated or claimed complete. Current sole primary-owned integration files are `config_values.py`, `test_config_values.py`, then configuration/main wiring; codec worker owns only `config_file.py` and `test_config_file.py`.
- Goal remains active. Full RBAC, workers, provider/profile integration, retained clients, generated docs, deployment and real controlled fax proof remain required release gates.

### Configuration value dependency reviewed

- Pure immutable values model is independently approved through `1f5c1f32` (initial `2caa819c`). It preserves79 existing fields/defaults and adds5 previously bypassed bootstrap/endpoint fields. It handles complete hybrid/inactive exports, aliases, inherited selectors/Sinch credentials, explicit empty/null/false/zero patches and safe validation errors. Seven focused behavior tests pass.
- Review found a short-secret/newline mask gap; reproduced and fixed before approval. Reviewer additionally rejected253 existing-helper mask examples across five secret fields, preserving the original frame and environment. Exact review is in configuration scratch `values-review.md`.
- This is an approved dependency, not active runtime behavior yet. The file codec and canonical DB/profile integration are underway; legacy settings callers remain to be replaced as one coherent activation path.

### Deployment topology clarified by the owner

- Faxbot is currently self-hosted/on-premises. `faxbot.net` hosts the demo/website and documentation; it is not the backend deployment target. The website may have a separate repository, to be located when its docs/publication work is reached.
- `faxbot-app` is the iOS companion, connecting to the operator's secured instance through WireGuard, Tailscale or Cloudflare Tunnel. Verify retained tunnel setup and mobile connectivity as part of self-hosted acceptance.
- The owner reports authenticated Cloudflare/Wrangler and Netlify CLIs. Moving DNS from Netlify to Cloudflare and optionally connecting through `faxbot.net` is lower priority and is not part of the critical implementation path. No DNS change is authorized or needed for this step.
- User's priority remains the finished self-hosted application first. A missing hosted backend URL is not a blocker or evidence that the app should run behind `faxbot.net`.

### Canonical configuration implementation in progress

- Literal file codec source `b96cc2e6` is independently approved:98 focused tests plus four isolated reviewer probes. It is an import/export dependency, not the active configuration authority.
- Migration `e537b630` adds immutable revision/profile tables, singleton state and explicit outbound/inbound binding tables without assigning historical jobs to current accounts. A real PostgreSQL VARCHAR20 failure drove provider-column widening to255; SQLite retains compatible historical declarations. Review found PostgreSQL NOT VALID foreign keys could hide orphan bindings; the regression fails before and passes after `e78775a8`, which checks catalog validation state. Independent migration review approves the corrected range, including a separate orphan-binding reproduction and all four provider columns retaining255characters.
- Encryption/key source `66296a3a` uses the pinned cryptography Fernet recipe with installation/kind/record identity inside each authenticated envelope. Fifteen focused tests cover tampering, wrong identities/key, invalid records, private key refusal, exclusive publication, durability uncertainty and six independent processes agreeing on one key. Independent review is underway. No hand-written cipher is used.
- Primary canonical-store implementation `7df58615` has ten passing tests across SQLite and PostgreSQL: first import is encrypted and authoritative on later initialization, missing key cannot be replaced, whole-candidate restart staging preserves active settings, stale edits conflict, and two independent processes cannot overwrite one revision. Commit-before/after acknowledgement failures reconcile through a fresh durable read. This store is not wired into request/provider/runtime callers yet; profiles, stopped-start promotion and full integration remain active work.
- A concurrent verification run exposed interference from the old PostgreSQL custom DEFAULT operator-class test: PostgreSQL selects that default database-wide despite schema isolation. `3d480e11` isolates that destructive test in its own temporary database and checks an ordinary shared-database migration while the custom default exists. This is test infrastructure correction, not a relaxed schema validator. Final complete API suite at this checkpoint:405 passed,14 expected dialect skips, zero warnings; no actual image verification of these new configuration components yet.

## Configuration review and GUI acceptance checkpoint

- Immutable provider profiles and atomic outbound job binding are implemented in `a40b8ef2`. Transaction cleanup/error review findings were corrected in `58828920`; independent correction probes pass on SQLite and PostgreSQL. Pending promotion requires the installation lifecycle lease. Lifecycle module `9c7780ea` has independent 30-test and three-probe approval.
- Bootstrap reconciliation and provider activation compilation are implemented in `b18a66eb`; catalog capture in `d4f055bd`. Independent review found masked custom credentials and invalid static HTTP headers could pass validation. Bounded corrections are underway. These new configuration modules still require production request/lifespan integration; they are not described as active behavior.
- `de317ec6` verifies original accepted profile credentials and manifest endpoint survive later edits using real loopback HTTP, on SQLite and PostgreSQL. This is module integration evidence, not real fax delivery.
- The broader API run found 658 passing tests and one test-peer TCP reset cleanup failure (14 dialect skips). `daf3566f` reproduces that reset deterministically and fixes the peer cleanup; all four focused reconnect/shutdown cases pass. Product AMI behavior is unchanged by that test correction.
- User requires all user-facing acceptance through real Browser/Computer clicks and keystrokes. Supplemental backend tests do not replace GUI acceptance. During inspection of an isolated local instance, Security and System Status dashboard shortcuts opened blank pages, login submitted while typing, and the All Statuses selection rendered blank. These and additional UI findings are recorded in the scratchpad for Sol correction and repeat GUI verification.
- Integration branch has been pushed and draft PR #32 opened: https://github.com/DMontgomery40/Faxbot/pull/32. No merge or production deployment. Hosted CI and docs generation results must be checked separately.
- Existing docs workflows target development/mkdocs, not every main commit. Docs Autopilot repair is in progress: fresh code-derived references and preview artifacts on the refresh branch, publication on every main commit. Until verified, agents use source instead of assuming historical docs describe current implementation.

### Hosted CI and GUI repair checkpoint

- Configuration catalog header validation and schema-aware plugin secret-mask findings are corrected in `e9d584c1` and `351964bf`. Independent correction review ran 226 passing focused tests. Canonical configuration remains pending runtime integration.
- First hosted Linux run exposed 23 subprocess tests depending on the runner working directory. `23a2e231` supplies the repository import root to child processes; the same clean-environment local reproduction then passed 128 tests. Hosted GitHub Actions run37080191139 passed **691 tests with14 dialect skips** on Linux with PostgreSQL16.
- UI fixes `60de89b8`, `0ade455e` and `223b430a` pass the combined Admin UI build. Primary real Browser replay verifies explicit Login/Enter, stored-key restoration, dashboard Security and SystemStatus navigation, factual Setup provider copy, Destination Number accessibility, and Jobs filtering Success -> AllStatuses restoring the queued synthetic job.
- Settings real GUI replay shows Audit/Persisted/Inbound controls matching loaded Disabled values; unsaved Enabled and numeric0 edits stay visible, and LoadSettings restores actual values. This proves editor behavior, not the still-pending canonical Apply integration.
- PDF download completion remains unresolved: clicking DownloadPDF produces no Browser download event or visible outcome in the in-app browser; an independent Sol agent reproduced that observation through real Brave clicks. No successful download or external fax delivery is claimed from the server's200 response.
- Added an Admin UI build job to PR/main CI in `147a7f86`; hosted execution is pending the next push.
- GUI review caught two documentation preview defects missed by a strict static build: generated pages offered an edit link to ignored output, and Redocly hydration produced a blank view. The edit action repair is GUI verified; API rendering correction and the clean hosted docs run remain in progress.

### Clean docs and browser acceptance established

- `4f3effef` restores automatic source-reference generation. Both hosted docs workflows passed; the actual downloaded artifact identifies its exact source commit and a clean tree. Primary MkDocs search/navigation and independent Brave API search/Send Fax navigation passed. No main/public-site publication is claimed while PR32 remains a draft.
- Independent review then identified a rename escape in optional docs patch validation and missing versioned canonical URLs. `e59247f0` repairs both with observed failing regressions. Full focused docs suite:24 passed; independent correction review:13 focused checks passed, approved.
- `28c7c1bb` and `24f28678` correct Setup/control labels and disabled-Inbox presentation. Independent Sol and primary real GUI replays passed. PDF download completion remains unverified; source/runtime configuration, full RBAC and real delivery work continue under the active goal.
- Evidence: [docs and GUI checkpoint](evidence/2026-10-02-docs-and-gui.md). Subsequent commits automatically regenerate the code reference; agents use source and matching provenance rather than historical prose.


## Canonical runtime integration checkpoint

The actual API now uses the durable configuration store for startup, request frames, settings/plugin edits and accepted outbound profile binding. The Settings editor uses desired revisions, durable Apply and stale-editor refusal; real Browser acceptance verified hot-value persistence, coordinated pending promotion after full fixture restart, explicit clears and concurrent-editor protection. See [evidence](evidence/2026-10-02-canonical-runtime.md). Combined internal configuration checks passed 123 tests. Remaining full-product gates in the evidence record are still active; this checkpoint does not mark the refresh complete.


## Canonical runtime: clean hosted checkpoint and GUI integration

At `f50fb62d`, hosted CI passes 741 tests with 14 dialect skips and the Admin UI build. Docs Autopilot and both MkDocs runs pass; the exact clean-source artifact was downloaded and its generated configuration reference checked through real Browser navigation. This supersedes the failed earlier runtime checkpoint, with legacy tests migrated to the canonical installation model and real acceptance fault injection retained.

Real GUI checks additionally cover active upload limits, queue-only acceptance, rejection when a stale queue-only form encounters enabled sending, installed manifest override acceptance, and feature-sensitive Tools navigation. Plugin editor revision safety and provider inventory corrections are the current bounded integration work. See the [canonical runtime evidence](evidence/2026-10-02-canonical-runtime.md). Durable dispatch/callbacks, complete RBAC, retained mobile/tunnel workflows, transfer/restore and real controlled delivery remain mandatory parts of the active whole-product goal.
