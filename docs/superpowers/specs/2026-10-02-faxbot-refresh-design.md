# Faxbot finished-product refresh

Status: revised after user review on October 2. User added complete RBAC, permitted UI improvements, and confirmed the existing TestFlight app. Implementation has not started.

## Outcome and constraints

Deliver a completed, verified Faxbot product for the October 7, 2026, 10:00 AM America/Denver handoff meeting. Retain the familiar Admin Console design as a starting point; the user explicitly permits UI upgrades where useful. Replace backend internals as necessary to make the existing product dependable and transferable. A demo, passing unit tests, or a backlog of known required repairs does not constitute delivery. Do not remove an existing feature or silently downgrade it to satisfy the deadline.

The existing product is self-hostable fax automation. The recommended deployment contract remains a dedicated installation per operating organization, with complete role-based access control (RBAC) for users and integrations, including scoped document access. Supporting independent companies in one shared installation would require an explicit tenant model and is a separate product decision; do not claim that existing global scopes provide tenant isolation.

The user requested GPT-6.1 Sol xhigh subagents for bounded work, with the primary agent retaining architecture, integration, and decisions requiring accumulated context. Subagents receive complete task briefs, relevant decisions, expected interfaces, test instructions, and evidence pointers. Use independent review after each complete slice. Never treat a subagent's completion claim as integration or release proof.

## Problem statement

The public website remains available, but main's backend and client integrations contain reliability defects and incomplete behavior. A recipient needs a working product they can install, operate, verify, and maintain. Existing work is distributed among main, development, iOS, desktop-platform, and documentation branches, so rebuilding only the checked-out files would lose product context.

Confirmed baseline: remote main matches local 7e0c15fa. An isolated Python 3.11 run passes 27 existing tests in disabled mode. A direct conversion probe with faxing enabled shows `contest.txt` replaced by a 19-byte placeholder. Code inspection identifies early inbound deduplication, in-process-only outbound scheduling, unguarded status writes, and inconsistent manifest provider selection. Inventory also confirms a Node MCP parse error. These observations establish repair requirements, not complete security or operational certification.

## Selected approach and alternatives

**Recommended: replace responsibilities inside a modular monolith.** Retain Python/FastAPI, the existing public HTTP contracts, SQLAlchemy, and the React UI. Move document integrity, inbound receipt, outbound delivery, provider configuration, and authorization into coherent modules with small interfaces. Each slice replaces the old behavior and earns its place through observable tests.

A patch-only approach is smaller but leaves recovery, configuration, and provider policy distributed across routes. A clean-sheet stack rewrite expands migration and compatibility risk without evidence the language or framework prevents a correct product. The selected approach permits substantial internal replacement without creating a second product to operate.

## User stories

1. As an operator, I can install Faxbot from a documented clean environment and reach the existing Admin Console.
2. As an operator, I can configure and validate a supported provider and see truthful capabilities.
3. As an operator, I can select different inbound and outbound providers without dispatching through the wrong adapter.
4. As a sender, I can upload supported documents without filenames, test flags, or missing dependencies replacing their contents.
5. As a sender, I receive validation errors before an invalid document is accepted for delivery.
6. As an integrator, I receive a stable job identity and can safely retry an identified submission.
7. As an operator, accepted work survives application restart and can be recovered without blind duplicate transmission.
8. As an operator, ambiguous provider outcomes remain visible and can be reconciled.
9. As an integrator, duplicate or out-of-order callbacks cannot regress terminal delivery state.
10. As an inbound user, I receive the actual document or an honest processing/failure state, never a success placeholder.
11. As an operator, transient inbound download/storage failures remain retryable and do not consume the only receipt opportunity.
12. As an administrator, I can create, rotate, revoke, and restrict keys consistently across HTTP and WebSocket routes.
13. As an authorized document reader, I can retrieve documents with the correct permission or explicitly supported expiring grant.
14. As an operator, I can change configuration safely without corrupting in-flight provider work or silently retaining old credentials.
15. As an administrator, I can inspect useful diagnostics, audit activity, and delivery errors without exposing secrets or document contents.
16. As an operator, retention, backups, restoration, and database upgrades preserve the intended records and artifacts.
17. As an SDK or MCP user, supported tools and transports load and work against the real HTTP contract.
18. As a desktop or mobile user, existing supported workflows continue to function against the refreshed backend.
19. As a maintainer, I can regenerate code-derived reference material and instructional docs through Docs Autopilot, OpenAPI/Redocly, MkDocs and Mike.
20. As a maintainer, each main commit triggers the intended documentation update and versioned publication, with visible failures rather than swallowed errors.
21. As the recipient, I can reproduce installation and verification without undocumented knowledge from the current owner.
22. As an administrator, I can manage users, groups, role assignments and permissions through working UI and backend flows.
23. As an administrator, I can grant only the actions and document/mailbox access a user or integration needs, and verify that direct HTTP/WebSocket calls enforce the same restrictions as the UI.
24. As an administrator, disabling a user or changing their role takes effect for active sessions and delegated keys, with audit evidence.
25. As a mobile user, my existing TestFlight app can connect, pair where supported, send, inspect status and receive documents without being forced into an unrelated redesign.

## Implementation decisions

### Product and branch reconciliation

Use main as the integration base and inspect development, iOS, and desktop branches for completed behavior and assets worth retaining. Do not blanket-merge historical branches with conflicting application, deployment, or documentation assumptions. Create an explicit capability matrix covering shipped, implemented, preview, mock-only, and incomplete capabilities. Existing advertised workflows that are incomplete receive implementation/verification work, not a silent deletion.

Preserve the HTTP paths, request fields, response fields, and UI behavior used by current clients. Additive fields are permitted. Any unavoidable public change needs an explicit compatibility adapter and client verification. User-facing security and recovery errors may become more precise; successful responses must remain compatible.

### Document integrity module

Real document processing is independent of file names and fax-transmission disablement. Disabled sending suppresses external dispatch, not document content. Tests inject substitutes explicitly. Generate valid PDFs from text with wrapping/pagination that does not silently truncate content. Parse/validate PDF and TIFF inputs using maintained tooling, bound resource usage, and reject unsafe names/paths. Conversion failures fail honestly and clean partial artifacts. No placeholder or empty artifact can satisfy a successful conversion or receipt.

### Inbound receipt module

Persist a durable receipt identity and processing state. Authenticate and validate provider events before acting on their document URLs. Separate accepted event, document acquisition, validated storage, and completed receipt states. Deduplication returns an existing outcome or resumes incomplete processing; it never suppresses retry after partial failure. Enforce the same semantics for cloud and internal telephony adapters. Download and conversion work has bounded time and size, protects against unintended network destinations, and validates actual content. Storage and database recovery is explicit across crashes.

### Outbound delivery module

Persist accepted jobs and attempt state before dispatch. A database-backed worker claims ready work atomically; a restart reclaims only work whose state permits safe recovery. Keep one deployment simple and support the documented database choices; do not add a distributed message broker without a demonstrated need. Persist submission identity, chosen provider/account identity, attempt identity, and transition history needed for recovery.

Do not blindly repeat an externally effective POST after an ambiguous failure. Use provider idempotency when available; otherwise reconcile or surface an explicit unknown outcome for operator resolution. Callbacks and submission replies use guarded transitions. Terminal results cannot regress to queued or in-progress. Client submission idempotency is additive and verifies that repeated keys describe the same request. Historical jobs continue to resolve through their original provider identity.

### Provider configuration module

One validated provider selection supplies dispatch, traits, inbound verification, diagnostics, and historical job lookup. Built-in and manifest adapters obey the same capability and error contracts. No unknown provider may fall through to SIP. Credential rotation invalidates cached adapters; reject or safely sequence configuration changes that would orphan active attempts. Configuration activation and persistence report failures clearly. Replace best-effort schema mutation with explicit, tested migrations and an upgrade path.

### Complete RBAC and operator controls

RBAC is mandatory release functionality, not future work or UI-only feature gating. Reconcile the identity and hierarchy work on historical branches; preserve useful concepts, replace scaffolding and unsafe session/authorization behavior. Model authenticated users and integration principals, roles composed of permissions, group membership and resource grants for document/mailbox access. Provide complete admin management flows, documented built-in roles and least-privilege defaults. Test role assignment changes, group/resource restrictions, delegation limits and protection against privilege escalation. Do not infer authority from arbitrary user-editable traits or possession of a generic key-management scope.

Use a single server-side policy decision for every protected operation, including direct HTTP calls, WebSocket sessions, SDK/MCP calls and mobile requests. UI visibility follows effective permissions but is never the enforcement mechanism. Persist sessions or make their lifecycle explicitly safe across worker/restart boundaries; disablement and revocation take effect promptly. Role and permission changes are auditable. Administrative host-shell access is a separately privileged operation. Legacy keys have an explicit migration/compatibility policy that cannot silently widen privileges.

Keep the dedicated-installation trust model explicit. Authentication is required in deployable production configuration; development/test behavior is explicit. Centralize authorization for fax actions, document access, key management, administrative settings and terminal operations. Distinguish host-shell permission from ordinary document or key-management permissions. Protect WebSocket origin/authentication and avoid credentials in URLs where clients permit safer exchange. Signed or tokenized document access has bounded scope and expiry. Test revocation, denied access, token expiry, and use after configuration changes.

### Clients and operational behavior

Repair existing SDK, MCP, Electron and mobile contract mismatches, then verify their supported workflows. Restore branch-only sources in a deliberate layout if needed. Use the familiar Admin Console as a baseline and improve its usability, information hierarchy and RBAC controls as needed; do not spend the deadline on an unrelated visual rebrand. Tunnel/pairing behavior must actually implement its promised behavior, with explicit configuration and restricted privileges. Never equate a generated code or label with a functioning tunnel.

### Documentation automation

Preserve the user's code-to-docs workflow. Treat OpenAPI, code, configuration schemas, and provider capabilities as reference sources of truth. Repair Docs Autopilot change detection to use the actual push range, validate generated changes, regenerate API reference with OpenAPI/Redocly, build MkDocs strictly, and publish versioned docs with Mike on main commits. Prevent recursive workflow loops and partial success from failed patch/build commands. Generated output records the source revision. Internal engineering specs and ledgers are not generated product documentation and must not be overwritten by the documentation pipeline.

## Testing decisions and proposed public seams

These are the proposed seams for approval before implementation:

- Existing HTTP and WebSocket interfaces: document acceptance, authorization, idempotency, jobs, receipts, settings, callbacks and document retrieval. Use real temporary databases and storage where practical.
- Document conversion interface: independent PDF text/page and TIFF inspection proves contents, pagination, validity and explicit failures. Tests must include filenames containing `test`.
- Worker lifecycle interface: start/stop/restart a real worker against durable job storage and a controlled provider simulator; observe outcomes through public job/receipt interfaces.
- Provider transport seam: realistic HTTP fixtures/simulators exercise authentication, request mapping, timeouts, ambiguous outcomes and callback validation; use real provider acceptance tests separately.
- Client interfaces and rendered UI: SDK calls, MCP initialize/list/call, browser workflows, desktop build and mobile simulator flows against the same backend.
- Deployment/docs commands: clean container install, migration, backup/restore, deterministic code-derived generation, strict documentation build, and main-commit publication with source revision verification.

Use one failing behavior test followed by its implementation, with independent review of each slice. Current disabled-mode tests are a compatibility baseline, not evidence of real conversion or fax delivery. Full-suite, build, rendered UI, deployed identity, and real fax receipt are separate gates. External verification uses synthetic documents and controlled send/receive destinations. Do not transmit real healthcare records or publish credentials.

## Completion gates

- Every retained capability in the reconciled product matrix has an implementation owner, a passing verification method, and recorded evidence.
- Complete RBAC management and enforcement is demonstrated for users, groups, roles, sessions, integration keys and document/mailbox resources, including denied direct calls and effective revocation.
- No known content-loss, duplicate-submission, lost-accepted-job, incorrect-provider, unauthorized-document-access, or false-success defect remains.
- Clean install and upgrade from a representative existing database succeed; backup and restore are demonstrated.
- Required regression tests, builds, integration checks and independent review pass against the exact release revision.
- The liked UI works in a real browser with the refreshed backend, and retained SDK/MCP/desktop/mobile workflows pass their applicable acceptance checks.
- Real controlled send and receive, callback progression, restart recovery and document fidelity are demonstrated for the release's supported integrations; simulator evidence is labeled separately.
- Documentation updates automatically on a main commit, builds and publishes successfully, and identifies the release revision.
- Release/deployment artifacts, runbook, credentials-transfer procedure, dependency/license inventory and recipient setup are complete and reproducible.
- Any unavailable external credential, endpoint, account, hardware or signing prerequisite remains an explicit unmet completion gate. It cannot be converted into a passing claim or silently removed from scope.

## Out of scope

An unrelated visual rebrand, inventing unrelated billing/CRM features, adding a new multi-tenant SaaS business model without a user decision, public outreach to prospects, legal/compliance certification claims, or replacing the language/framework solely for fashion. These exclusions do not exclude repairs needed for the existing product to function.

## Execution and continuity

The full refresh is one goal with staged subsystem specifications and complete vertical tasks. Keep a durable acceptance matrix, decision record, task graph and evidence ledger in the integration branch. Use GPT-6.1 Sol at xhigh for bounded implementation and independent review. The primary agent owns cross-module concurrency design, ambiguous provider behavior, branch reconciliation and integration decisions, as requested.

Main is the intended integration target. PR review/CI/merge and deployment must be tracked separately from local tests. Respect existing user authorization, and ask only for material product decisions or truly missing external prerequisites. This proposed design is not implementation completion.
