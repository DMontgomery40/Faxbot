# Configuration store and runtime integration plan

> **For agentic workers:** Use superpowers:executing-plans for primary implementation and superpowers:subagent-driven-development for bounded independent review. User has approved the whole refresh; no repeated permission step.

**Goal:** Activate and persist one validated configuration revision, preserve immutable provider/account bindings for accepted work, and expose truthful pending restart state through existing clients.

**Architecture:** Implement the reviewed `../specs/2026-10-02-faxbot-configuration-activation.md`. Existing database becomes activation authority. Frozen0003 metadata adds configuration revisions, provider profiles, singleton head and explicit job/receipt binding tables; legacy jobs remain unbound. A private installation key encrypts versioned payload envelopes with authenticated identity. All public callers use the manager rather than manual environment mutation. The values and file-codec dependencies are separate reviewed components, not alternate stores.

## Migration and durable store — primary

- [x] RED: real SQLite/PostgreSQL upgrade creates revision/profile/head/binding contracts, preserves all historical rows and leaves their account bindings absent. Prove valid manifest IDs longer than20characters can be stored without truncation on both databases.
- [x] Add0003 frozen metadata and extend revision-specific pre/postflight checks. Preserve frozen0001/0002. Widen provider columns toVARCHAR255 on PostgreSQL; retain SQLite's unconstrainedVARCHAR20 declarations to avoid destructive table rebuilds, with explicit ORM dialect types. Reject unsupported extension tables/constraints before mutation. Exercise rollback, repeat upgrades, invalid extensions and FK integrity.
- [ ] Implement installation-key loading/exclusive durable creation with missing-key refusal for existing encrypted data. Use the pinned cryptography recipe, authenticated envelope identity/version checks and safe errors. No key in database, logs, responses or generated docs.
- [ ] Implement immutable DB revisions/profiles, first import, hot Apply and whole-candidate staging, CAS generation/desired identity, failed/uncertain commit reconciliation, and profile reads. Validate payload before locks; no network or conversion under configuration lock.
- [ ] Test actual two-process reads/updates and configuration-versus-acceptance serialization using both supported databases. Historical unbound records are not assigned to current credentials.

## Runtime and HTTP integration — primary

- [ ] Replace legacy Settings factories/manual env mutation/export with the value model and canonical manager. Preserve stable imported settings facade and capture an immutable operation frame.
- [ ] Implement stopped-start lifecycle locking and candidate resource readiness before promotion. Ordinary reload or one-worker restart does not promote pending configuration. Fail clearly and retain active state when preparation fails.
- [ ] Preserve GET nested/PUT flat/settings metadata/plugin route contracts, complete inactive/hybrid credentials, imports/exports and actual configured paths. Repair stale VALID_BACKENDS readiness, invalid-selector fallbacks and plugin enable/settings divergence.
- [ ] Bind document preparation/acceptance/current background provider calls/status to stored profiles; remove per-call environment reload and stale credential caches. Use original manifest snapshot and account binding. Legacy unresolved jobs remain explicitly unbound/reconcilable.
- [ ] Repair every supported Settings/Setup field and pending/active UI wording. Bootstrap DB transfer remains a real maintenance operation, not a live URL text mutation.

## Verification — primary and independent reviewer

- [ ] Meaningful HTTP tests cover invalid mixed patches, clear/null/false/zero, masks, hybrid providers, plugin role disable, concurrent edits, failed publication and old-job lookup after rotation.
- [ ] Full API suites on SQLite/PostgreSQL and actual committed API image; clean startup, repeat startup, safe missing-key refusal, export/reload, pending restart and provider-frame behavior.
- [ ] Independent fixed-range review and corrections. Retain exact artifact/source/test identities in versioned evidence.
- [ ] Complete configuration is not claimed until these integrations pass. Durable dispatch/receipt state machines, complete RBAC, all retained client/docs/deployment/real-fax gates remain part of the same goal.
