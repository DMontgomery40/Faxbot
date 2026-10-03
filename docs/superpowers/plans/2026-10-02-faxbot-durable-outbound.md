# Faxbot Durable Outbound Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development for bounded tasks and review; the primary owns architecture, delivery state and integration. Steps use checkbox syntax for tracking.

**Goal:** Accepted outbound faxes survive restarts without duplicate transmission, keep their original provider identity, and expose truthful recovery controls.

**Architecture:** A database-backed delivery module owns atomic acceptance records, claims, attempt transitions and event history. Lifespan workers submit through captured adapters; callbacks and polling use the same guarded transitions. Request handlers no longer own delivery background tasks.

**Tech Stack:** Existing Python 3.11/FastAPI/SQLAlchemy/Alembic, SQLite and PostgreSQL, existing React/MUI admin UI. No message broker.

**Spec:** `../specs/2026-10-02-faxbot-durable-outbound.md`, subordinate to `../specs/2026-10-02-faxbot-refresh-design.md` and canonical configuration invariants.

## Global constraints

- Preserve successful existing HTTP/client contracts; additive delivery fields and Idempotency-Key are allowed.
- Use immutable accepted revision/profile for dispatch, polling and callback verification.
- Never automatically submit held or historical/ambiguous work.
- All user-facing acceptance uses Browser/Computer real clicks and keystrokes; backend checks are supplemental.
- Only synthetic local transport fixtures until a controlled real provider/destination is established.
- Keep SQLite and PostgreSQL support; new frozen additive migration, no rewriting prior migrations.
- Keep sensitive provider data out of transition history and errors.
- No unrelated DNS/public-site changes; full RBAC and inbound receipts remain required subsequent slices.

## Review focus

1. Crash between durable submission marker and external response must leave uncertainty, never retry.
2. Stale claimed worker after lease expiry must be fenced before external submission.
3. Callback before send acknowledgement and conflicting terminal events must not regress result or attach another provider fax.
4. Idempotency replay after provider rotation must return the original request rather than send through the new account.
5. Previously queued disabled-mode or legacy work must not escape into the new worker.

## Task 1 — Additive schema and transactional delivery state

**Primary files:** new `api/app/schema_outbound.py`, `api/alembic/versions/0004_outbound_delivery.py`, `api/app/outbound_store.py`; modify `schema.py`, `alembic/env.py`, `config_store.py`. Internal tests `api/tests/test_outbound_store.py` and focused migration coverage in `test_schema.py`.

**Interface:** `OutboundStore` owns acceptance delivery insertion (inside the existing config acceptance transaction), claim/prepare-to-submit transitions, outcome/event recording, conservative expired-lease recovery, reconciliation and history reads. Return immutable claim objects containing job, attempt, token, captured revision/profile identity; callers cannot manufacture a successful transition by passing an arbitrary current profile. Exact Python signatures are finalized in primary source before any dependent agent implementation is dispatched.

- [x] Reproduce missing durable acceptance and duplicate-claim behavior with pure store tests against real SQLite and PostgreSQL.
- [x] Add frozen 0004 tables and validator support for earlier revisions; preserve historical rows/artifacts/auxiliary references and refuse malformed extension schemas.
- [x] Integrate delivery insertion into configuration acceptance's existing transaction; keep acknowledgement uncertainty and artifact-retention contract.
- [x] Implement short atomic claims, fencing, events, terminal guards and conservative recovery; classify earlier nonterminal jobs for reconciliation without guessing account identity.
- [x] Prove two independent workers produce one claim; expired preparation fences its old worker; expired submission never becomes ready; callbacks/replies cannot regress terminal results; rollback/uncertain acknowledgements cannot create invisible side effects.
- [x] Commit bounded source and run independent fixed-range review; repair actionable findings before transport integration.

## Task 2 — Captured transport submission and lifespan worker

**Primary files:** new `api/app/outbound_worker.py`, `api/app/outbound_transport.py`; modify `main.py`, `provider_execution.py`, provider-specific adapters only as needed. Internal tests `api/tests/test_outbound_worker.py` use controlled adapters and real durable store.

- [x] Replace per-provider direct FaxJob writes with typed normalized receipts/categorized failures at the transport seam.
- [x] Start an owned worker after canonical runtime readiness; stop/join before releasing AMI/config lifecycle resources.
- [x] Dispatch only current claimed normal-mode work; persist submission marker before transport, and prohibit retry after ambiguous errors/cancellation.
- [x] Capture accepted document/token/callback settings; never resolve newer global credentials for old work.
- [x] Remove request-bound BackgroundTasks dispatch and redundant status mutation paths once the owned worker is integrated.
- [ ] Verify process restart, submission timeout, lease loss, late response and shutdown at each internal crash window. Inspect actual source for all remaining FaxJob writers.
- [x] Real GUI submission against a local provider simulator: accepted document, visible worker progression, restart recovery and uncertainty presentation. Record actual clicks/screenshots and simulator attribution.

## Task 3 — Idempotent acceptance and guarded status/callback integration

**Primary files:** `documents.py`, `main.py`, `config_store.py`, `outbound_store.py`, `outbound_transport.py`; provider signature helpers as required. UI adapter additions assigned to one Sol writer after exact response shape is fixed.

- [x] Hash original bytes while streaming; add a versioned request fingerprint and stable authenticated principal scope.
- [x] Accept optional bounded Idempotency-Key, persist its digest/scope with job acceptance, reject mismatched reuse, return original job on identical replay and remove duplicate temporary artifacts only.
- [x] Verify provider signature contracts from official sources before replacing callback verification; use captured account/profile and bounded matching.
- [x] Route AMI, FreeSWITCH, HTTP callbacks and polling through one guarded event interface; preserve callback URLs with additive identifiers where supported.
- [x] Prove duplicate/out-of-order callbacks, callback-before-acknowledgement, provider rotation, SID mismatch, terminal conflict and idempotency concurrency through internal seams.
- [ ] Real GUI replays cover safe resubmission/retry affordances and visible terminal/conflict states; no direct API acceptance substitute.

## Task 4 — Operator recovery, retention and release checkpoint

**Primary backend, bounded Sol UI:** `main.py`, `outbound_store.py`, `api/admin_ui/src/components/JobsList.tsx`, plugin/client types where required.

- [ ] Expose delivery state, captured provider identity, attempt history and actionable reconciliation reason in Jobs/detail.
- [ ] Provide version-checked, audited administrative reconciliation that cannot implicitly resend ambiguous work; do not add a blind Retry button.
- [x] Ensure retention preserves nonterminal/held/unknown documents and uses captured artifact locations.
- [ ] Sol performs real GUI recovery flows and records every additional UI defect; fix and repeat the same path.
- [ ] Independent whole-slice source review, relevant internal verification, actual container restart acceptance and matching pushed CI/docs artifact.
- [ ] Update versioned evidence/ledger with exact proof limits. Continue inbound receipts, complete RBAC, retained clients, transferability and controlled real fax delivery under the existing goal.
